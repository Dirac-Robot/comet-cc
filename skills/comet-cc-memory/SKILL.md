---
name: comet-cc-memory
description: Search, read, and cross-reference the persistent memory store that the CoMeT-CC plugin has been building across this and prior Claude Code sessions. Use when the user references "before", "earlier", "last time", "that thing you worked on", "you already did…", or when you have a hunch that a relevant decision/artifact/correction exists but is no longer visible in the current transcript. Also use when a new task looks like something you've solved before (migration patterns, failed approaches, user preferences, constraints).
---

# CoMeT-CC memory recall

The active Claude Code session has a sidecar memory pipeline running as
hooks + a daemon. After every compaction trigger (topic shift, high
cognitive load, or buffer overflow), a structured `MemoryNode` is written
to a sqlite store with summary / trigger / importance / tags. Across
sessions, these accumulate into a long-term store you can query via the
`comet-cc` CLI.

**Passive injection already happens.** Each user prompt, the hook pulls
relevant passive + active matches into your context automatically. This
skill is for the cases where that automatic retrieval missed something —
either because the match threshold didn't fire, or because you want to
deliberately explore a thread.

## When to invoke this skill

- User says "remember when…" / "you fixed this before" / "last time we…"
- You're about to repeat work that feels familiar (same file, same bug class)
- A user correction/preference might apply but you aren't sure
- Starting a new task that could reuse a pattern from an earlier session
- Debugging — earlier failure modes or tried-and-rejected approaches are worth checking

## Operations (run with the Bash tool)

### Search across all memory

```bash
comet-cc search "<free-form query>" --top 5
```

Returns up to N matching nodes with summary + trigger. Examples:

- `comet-cc search "async SQLAlchemy migration" --top 5`
- `comet-cc search "user's logging preferences"`
- `comet-cc search "tests that were failing" --session <session-id>`

Scope to the current session with `--session <id>` when cross-session
matches would just be noise (id appears in some transcript
headers; if you don't know it, omit the flag).

### Read a specific node in full

```bash
comet-cc read-node <node_id>
```

Shows the node's full summary, trigger, tags, importance, recall mode,
and why it was compacted (topic_shift / high_load / buffer_overflow).
Use this after `search` surfaces a promising `[n_…]` id.

### List every node in the current session

```bash
comet-cc list-session <session_id>
```

Chronological timeline of the session's compacted nodes. Useful when the
user asks "what have we done so far" or you want to audit coverage.

### Read the session brief

```bash
comet-cc brief <session_id>
```

The buttered brief is a live-rewritten document summarizing the
session's durable preferences, active work context, and hints from
failures/corrections. It rides in every turn's context, but you can dump
the current state here if you want to inspect or reason about it.

## How to interpret results

Each node has two distinct fields:
- **summary**: factual index — what's stored (decisions, artifacts, preferences)
- **trigger**: retrieval scenario — when the raw context becomes worth reopening

Treat `trigger` as the retrieval intent: if it matches your current
decision point, the node is worth considering. If `summary` answers your
question directly, you may not need to dig further.

Nodes tagged with `IMPORTANCE:HIGH` are either persistent user
constraints, corrections, or artifacts the user will likely reopen —
prioritize these. `recall_mode: passive` means it's always in context
already (double-check the injected memory block before redundantly
citing).

## Style

When you apply a recalled node, tell the user briefly where it came from
so they can verify: "based on the memory node [n_abc...] from earlier
this session…" One sentence of attribution is enough — don't re-render
the whole node.

Don't over-use the search. If automatic passive injection already gave
you the fact you need, acting on it is fine. This skill is for the
cases where that injection missed.
