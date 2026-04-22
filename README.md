# CoMeT-CC

Persistent memory plugin for Claude Code. A sidecar pipeline that builds
structured summaries across sessions and feeds them back via hooks, a
skill, and a warm daemon — all driven through `claude -p` subprocesses.
No external API keys.

## What it does

Claude Code sessions are ephemeral. When the window fills, CC's native
compactor compresses context into a short summary and anything older
effectively disappears. CoMeT-CC rides alongside each session:

- A **haiku sensor** inspects every assistant turn for topic shifts,
  cognitive load, and redundancy.
- On trip, a **sonnet compacter** produces a structured `MemoryNode`
  (summary, trigger, tags, importance, recall mode) plus a rolling
  session brief.
- Nodes land in a local **sqlite store** with BGE-M3 multilingual
  embeddings, indexed across every session on the machine.
- The **UserPromptSubmit** hook retrieves passive + active matches and
  injects them as `additionalContext` before Claude answers.
- The **PreCompact** hook feeds the same anchors to CC's compactor so
  its summary is grounded in pre-digested material instead of cold
  re-summarization.
- A **skill** (`comet-cc-memory`) + CLI (`search / read-node /
  list-session / brief`) give the model an active-recall path when the
  automatic injection misses something.

A long-lived **daemon** keeps BGE-M3 warm and owns the sqlite store.
Hooks are thin Unix-socket RPC clients (~30ms warm) with an in-process
fallback path so a missing daemon never breaks CC.

## Install

```bash
pip install -e .
comet-cc install
```

`comet-cc install` registers four hooks in `~/.claude/settings.json`
and copies the skill into `~/.claude/skills/comet-cc-memory/`:

| Hook              | Role                                                          |
|-------------------|---------------------------------------------------------------|
| SessionStart      | Spawn the daemon if not running (preloads BGE-M3 once)        |
| UserPromptSubmit  | RPC retrieval → `additionalContext` with brief + matched nodes|
| Stop              | RPC `queue_compact` — fire-and-forget to the daemon worker    |
| PreCompact        | RPC fetch → `systemMessage` with anchors for CC's compactor   |

## CLI

```bash
comet-cc daemon start | stop | status
comet-cc status                             # hooks + skill + daemon state

# Memory operations (also invoked by the skill from inside CC)
comet-cc search "<query>" [--session <id>] [--top N]
comet-cc read-node <node_id>
comet-cc list-session <session_id>
comet-cc brief <session_id>
```

## Tradeoffs vs the full CoBrA harness

CoMeT-CC is intentionally narrower than the full CoBrA harness. Three
concessions fall out of CC's protocol — a plugin can augment context
but cannot fully own it.

### 1. Compact-time effect only

In the full harness, summarization replaces raw turns the moment a node
is produced — the message array is rewritten in place. In a CC plugin,
the running Claude's in-memory transcript is not reachable to external
processes. Raw substitution only happens when CC itself auto-compacts
(at its token threshold) or the user invokes `/compact`. Node
generation here is continuous in the background; its *visible* effect
on what Claude sees waits for that compaction event.

### 2. Anchors alongside CC's compaction, not replacing it

When CC compacts, its own summarizer runs. We inject our pre-digested
summaries via PreCompact `systemMessage` so CC's compactor can anchor
on them instead of re-summarizing cold. But CC's compactor is still
the one that writes the compacted turn — we can *guide* the output,
not supply it directly. The full harness owns the compaction output.

### 3. Lightweight memory harness

The full harness covers many modalities (dialog / code / execution
traces / external APIs / images / session handoffs) and runs a
post-session consolidation stage (dedup, cross-session link graph,
cluster synthesis, lessons extraction). CoMeT-CC keeps only what a
CC session benefits from day-to-day: two modalities (dialog / code),
passive/active/both recall modes, summary/trigger semantics, vector
search. Batch consolidation is deliberately out of scope.

When the above three matter (true turn-time replacement, authoritative
compacted output, rich cross-session graph), use the full harness
directly. CoMeT-CC is for bolting a persistent memory layer onto an
otherwise native Claude Code workflow.

## Configuration

| Env var                       | Default       | Role                                       |
|-------------------------------|---------------|--------------------------------------------|
| `COMET_CC_HOME`               | `~/.comet-cc` | Store + L1 buffers + logs root             |
| `COMET_CC_MAX_L1`             | `20`          | Hard cap on buffer turns (overflow gate)   |
| `COMET_CC_MIN_L1`             | `3`           | Minimum buffer before compaction considered|
| `COMET_CC_LOAD_THRESHOLD`     | `4`           | Sensor load (1–5) that trips compaction    |
| `COMET_CC_MAX_CONTEXT_NODES`  | `8`           | Retrieval cap injected per turn            |
| `COMET_CC_MIN_SIM`            | `0.30`        | Cosine floor for active-node matching      |

## Testing

Layered tests, running from fastest to heaviest:

```bash
python tests/smoke.py --no-embed     # Pure-module regression, no LLM, no embedder
python tests/smoke.py                # + BGE-M3 embedder roundtrip
python tests/real_llm.py             # + claude -p sensor + compacter calls
python tests/hook_dryrun.py          # Synthetic payloads into each hook
python tests/daemon_multipass.py     # Buffer monotonicity across compactions
python tests/multiturn_live.py       # Full live CC session, 3 topic shifts
```

`multiturn_live.py` is the end-to-end check — drives CC through 7 turns
via headless `claude -p` + explicit `--resume <session_id>`, polls the
daemon's `queue_depth` until idle, then asserts store state and
auto-injection behavior.

## Status

`0.0.1` — alpha. Core pipeline verified end-to-end (see
`tests/multiturn_live.py`). Long-term drift and edge-case behavior are
not yet broadly dogfooded.
