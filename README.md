# CoMeT-CC

Persistent memory layer for Claude Code. Runs as a local TLS proxy that CC
routes through via `ANTHROPIC_BASE_URL`; every `/v1/messages` request is
inspected, optionally rewritten (raw turns replaced with a running
summary), and forwarded to Anthropic. Drives summarization with `claude -p`
subprocesses so there are no external API keys to manage — everything
bills against your existing Claude subscription.

> **Built on [CoMeT](https://github.com/Dirac-Robot/CoMeT)** — the
> Cognitive Memory Tree system. A few things CoMeT brings to the table
> that CoMeT-CC inherits directly:
>
> - **Effectively infinite context window.** Agents read at the
>   *shallowest* tier that answers the question — summary (T1) by
>   default, detailed summary (T2) when specifics matter, raw (T3)
>   only when verbatim is required. Conversation length stops being a
>   token-cost problem; what matters is what you actually drill into.
> - **Lossless structured memory.** Summaries *index*, raw is
>   *preserved*. Compact isn't a one-way compression — every compacted
>   node stores the absorbed turns verbatim as tier 3.
> - **Dual-speed sensor + compacter pipeline.** Cheap haiku sensor gates
>   every turn for topic shift / cognitive load / redundancy; expensive
>   sonnet compacter only runs when a gate trips. Per-turn overhead
>   stays low even as context grows.
> - **Hierarchical tool-bundle synthesis.** A turn with N tool calls
>   becomes ONE bundle-parent node (visible in the memory map) plus N
>   linked child nodes (drill-down). Tool noise never fragments the
>   dialog layer.
> - **Node-graph retrieval.** Memory is a linked graph, not a flat set.
>   Similarity cross-links are added automatically on save, and
>   retrieval walks `links` one hop from each direct match so a query
>   surfaces its semantic neighborhood, not just the single closest node.
>
> CoMeT-CC ports sensor, compacter, 3-tier storage, tool-bundle
> synthesis, similarity cross-linking, and retrieval graph expansion
> down to a single-session scope suitable for a Claude Code plugin.
> What it *doesn't* carry from the full CoMeT: post-session
> consolidation (dedup + cluster synthesis), multi-modality pipelines
> (external content, file embedding), and session handoff. If your use
> case needs any of those, go to
> [CoMeT](https://github.com/Dirac-Robot/CoMeT) or
> [CoBrA](https://github.com/Dirac-Robot/CoBrA) (which sits on top).

## How it works

Every outgoing `/v1/messages` request passes through the proxy. Per-turn
flow:

1. Parse the request; find the session_id in `metadata.user_id`.
2. Convert `messages[]` to per-message fingerprints (for rewrite tracking)
   and a bundled view (tool_use + tool_result chains collapsed) for the
   sensor.
3. **Summary rewrite** — if a summary exists for this session, absorbed
   messages are dropped from the outgoing array and replaced with a
   `(user, assistant)` summary pair. CC's local transcript is untouched;
   only what Anthropic sees shrinks.
4. **Retrieval injection** — passive/both nodes + vector-matched active
   nodes + session brief ride as a `<system-reminder>` block on the last
   user message. Graph expansion surfaces 1-hop neighbors of the top
   matches at a relevance decay.
5. **Sensor check** (throttled, async) — if the unabsorbed buffer crosses
   the min size, a haiku call assesses the latest turn against the buffer
   tail. On a trip (`topic_shift` / `high_load` / `buffer_overflow`),
   the compact worker fires.
6. **Compact** — sonnet produces a structured `MemoryNode` (summary,
   trigger, tags, importance, recall_mode) plus a rolling session brief.
   Tier-3 raw turns are stored verbatim in a side table.
7. **Bundle synthesis** — if the compacted buffer contained any
   tool_bundle entries, an extra haiku call produces a bundle-parent
   summary + per-call children, saved with `parent_node_id` + `links`
   so the memory map shows only the parent.
8. **Cross-link** — the new node's embedding is cosined against same-
   session active peers; anything above threshold gets a bidirectional
   `links` edge, building the graph incrementally.

A long-lived **daemon** hosts the warm BGE-M3 embedder, the NodeStore,
the trim worker, a Unix-socket RPC endpoint (for skill + CLI + graph
view), and the TLS proxy itself — all in one process.

## Install

```bash
pip install -e .
comet-cc install
comet-cc daemon start
```

`comet-cc install` generates a self-signed CA under `~/.comet-cc/certs/`
and copies the skill into `~/.claude/skills/comet-cc-memory/`. Then run
CC through the proxy:

```bash
comet-cc run claude -p "hello"
# or, for an interactive session:
comet-cc run claude
```

Under the hood this sets:

```
ANTHROPIC_BASE_URL=https://127.0.0.1:8443
NODE_EXTRA_CA_CERTS=~/.comet-cc/certs/ca.crt
```

and execs whatever command follows `run`. If you'd rather export those
yourself (e.g., in a shell profile or IDE integration):

```bash
eval "$(comet-cc env)"     # current shell
claude                     # now goes through the proxy
```

To turn the proxy off, just run `claude` without those variables (or
`comet-cc daemon stop` to free the port entirely).

### GUI editors (VSCode / JetBrains / Cursor)

The CC extension spawns the same `claude` binary under the hood, so any
route that puts `ANTHROPIC_BASE_URL` + `NODE_EXTRA_CA_CERTS` into its
process environment works. The bundled helper handles this end-to-end:

```bash
scripts/setup-gui-env.sh install            # this boot only
scripts/setup-gui-env.sh install --persist  # + LaunchAgent (autostart daemon, survives reboot)
scripts/setup-gui-env.sh status
scripts/setup-gui-env.sh uninstall
```

What `install` does, per OS:

- **macOS** — `launchctl setenv` for the current boot (visible to GUI
  apps immediately), plus a managed block appended to `~/.zshenv` for
  future terminals. `--persist` adds a LaunchAgent at
  `~/Library/LaunchAgents/io.cometcc.daemon.plist` that autostarts the
  daemon on login and exports the env vars to every process launched
  via launchd.
- **Linux / other** — a managed block appended to `~/.profile` (read by
  most display managers so GUI apps inherit it). Log out + back in to
  pick up the change.

After running `install`, quit + relaunch your editor so it inherits the
new environment. Verify with `scripts/setup-gui-env.sh status` (should
show `✓ ANTHROPIC_BASE_URL=…`), then watch `~/.comet-cc/logs/daemon.log`
while chatting — `trim[...]` and `injected retrieval` lines mean the
proxy is in the path.

## CLI

```bash
# Lifecycle
comet-cc install           # CA + skill
comet-cc uninstall         # remove skill (cert + store kept)
comet-cc status            # cert, skill, daemon, proxy state
comet-cc env               # print ANTHROPIC_BASE_URL + NODE_EXTRA_CA_CERTS
comet-cc daemon start | stop | status
comet-cc run <cmd> [args...]

# Memory operations (also invoked by the skill from inside CC)
comet-cc search "<query>" [--session <id>] [--top N]
comet-cc read-node <node_id>                 # depth 0: summary + trigger
comet-cc read-node <node_id> --depth 1       # detailed summary (lazy-generated, cached)
comet-cc read-node <node_id> --depth 2       # tier-3 raw turns, verbatim
comet-cc read-node <node_id> --links         # append linked children
comet-cc list-session <session_id>           # parent nodes only
comet-cc list-session <session_id> --all     # include bundle children
comet-cc brief <session_id>                  # rolling session brief

# Visualization
comet-cc graph             # web knowledge-graph view (opens in browser)
```

### 3-tier read semantics

Pick the lowest depth that answers the question — escalating wastes tokens
and clock time:

| Depth | Cost | Use for |
|---|---|---|
| 0 — summary + trigger | free | factual questions the retrieval block already covers |
| 1 — detailed summary (haiku, cached) | ~2–5s first call, free thereafter | specifics the T1 summary glossed over |
| 2 — raw turns, verbatim | free (sqlite read) | exact words: user instruction / error message / code snippet |

### Graph view

`comet-cc graph` spawns a local web server on `http://127.0.0.1:8450/`
and opens your browser to an e-ink-styled force-directed visualization of
the store. Solid edges = similarity cross-links; dashed arrows = parent →
child. HIGH nodes filled, MED hollow, LOW / bundle-children dimmed.
Click any node → right panel shows summary, trigger, tags, metadata,
linked children, peer links, and the full tier-3 raw turns (scrollable).
Runs in the foreground; Ctrl-C to stop.

## Design notes

### Why a proxy instead of hooks

An earlier version used CC's hook system (SessionStart /
UserPromptSubmit / Stop / PreCompact). That path had two structural
limits: (1) raw turns could only be replaced at CC's own compact
boundary, because hooks can augment but not rewrite messages in flight;
(2) anchors went in *alongside* CC's compactor rather than replacing
its output. The proxy lifts both — every outgoing request passes
through our listener, so a summary pair can substitute absorbed turns
at any turn, and CC's native compactor effectively never has to run
(the upstream model only ever sees the compacted view). The hook
version is archived on the `hook-arch-archive` branch.

### /compact is intercepted

Because the proxy already manages summarization, the plugin disables
CC's native `/compact` command: any request matching the native
compactor's prompt signature gets short-circuited with a 400 error and
an explanatory message. This keeps the two summarizers from clobbering
each other's state. If you truly want CC's native compactor, stop the
daemon (`comet-cc daemon stop`) — the proxy is gone, CC talks directly
to Anthropic, and `/compact` works as usual.

### Session scoping + cross-session toggle

Retrieval is scoped to the current session by default — CoMeT-CC
doesn't try to support session handoff, so leaking another session's
memory into a fresh one would be surprising. Cross-session is available
as an opt-in:

```bash
export COMET_CC_CROSS_SESSION=1
```

With the toggle on, passive/active retrieval spans every session in the
store; useful if you want long-running preference or rule nodes to
survive `/compact`, `/clear`, or `--resume` sequences that rotate
session_ids.

### Lightweight vs full CoMeT

Inherited: sensor, compacter, 3-tier storage, tool-bundle synthesis,
similarity cross-linking (`add_bidirectional_link`), retrieval 1-hop
graph expansion. Dropped: post-session consolidation (dedup, cluster
synthesis, lessons extraction), multi-modality pipelines (external
content, file embedding), session handoff / inherited memory. If those
matter for your workflow, reach for the full
[CoMeT](https://github.com/Dirac-Robot/CoMeT).

### Sensor input truncation (Claude-only constraint)

One deliberate divergence from CoMeT: the sensor sees only
*truncated* L1 content — `[role] text[:500]` per buffer entry, last 5
entries, plus the current turn capped at 4000 chars. The full CoMeT
sensor sees every entry at full length.

Why: CoMeT itself can use any cheap SLM as its sensor (gpt-4o-mini at
$0.15/M, Claude 3 Haiku at $0.25/M, or a local Ollama model for free),
so feeding it a full 200K-token coding turn every single turn costs
nothing meaningful. CoMeT-CC can't — it runs inside Claude Code via
`claude -p`, and the cheapest model Claude Code offers is the current
Haiku, which is billed against your Claude subscription quota unit-for-
unit with real tokens. Sending 200K × (K+1)/2 tokens to the sensor on
every single turn would burn quota (and, equally important, stall the
turn loop — haiku over 1M tokens takes 30s+, which is longer than the
user's gap between turns, so the sensor would never catch up).

The truncation keeps sensor latency at ~3-5s and quota impact near
zero, at the cost of some topic-shift detection sensitivity on turns
whose signal lives past the first 500 chars. If you'd rather trade
latency for accuracy, bump the limits in `comet_cc/proxy/extractor.py`
(`text[:500]`, `raw_content[:4000]`, `SENSOR_BUFFER_TAIL=5`).

## Configuration

| Env var                        | Default                       | Role                                                                 |
|--------------------------------|-------------------------------|----------------------------------------------------------------------|
| `COMET_CC_HOME`                | `~/.comet-cc`                 | Store + certs + logs root                                            |
| `COMET_CC_PROXY_HOST`          | `127.0.0.1`                   | Bind address for the TLS listener                                    |
| `COMET_CC_PROXY_PORT`          | `8443`                        | Port for the TLS listener                                            |
| `COMET_CC_UPSTREAM`            | `https://api.anthropic.com`   | Where the proxy forwards                                             |
| `COMET_CC_MAX_L1`              | `20`                          | Hard cap on unabsorbed buffer turns                                  |
| `COMET_CC_MIN_L1`              | `3`                           | Minimum buffer before compaction considered                          |
| `COMET_CC_LOAD_THRESHOLD`      | `4`                           | Sensor load (1–5) that trips compaction                              |
| `COMET_CC_MAX_CONTEXT_NODES`   | `8`                           | Retrieval cap injected per turn                                      |
| `COMET_CC_MIN_SIM`             | `0.30`                        | Cosine floor for active-node matching                                |
| `COMET_CC_CROSS_SESSION`       | `0`                           | `1` to retrieve across all sessions (default: scope to current)      |
| `COMET_CC_CROSS_LINK_SIM`      | `0.55`                        | Min cosine similarity to auto-add bidirectional `links` on save      |
| `COMET_CC_CROSS_LINK_TOP_K`    | `10`                          | Max peers to cross-link to per new node                              |
| `COMET_CC_HOP1_DECAY`          | `0.5`                         | Relevance decay for retrieval's 1-hop neighbors (0 disables)         |

## Troubleshooting

| Symptom | Fix |
|---|---|
| `certificate verify failed` from CC | `NODE_EXTRA_CA_CERTS` not propagated. Use `comet-cc run claude …` or `eval "$(comet-cc env)"`. |
| `connection refused` | Daemon isn't up. `comet-cc daemon status`; if down, `comet-cc daemon start`. |
| Trim never fires on your expected turn | Haiku sensor is non-deterministic on short/related topics. Lower `COMET_CC_MAX_L1` to force the overflow path, or watch `~/.comet-cc/logs/daemon.log` to see what it judged. |
| Stale/unwanted summary keeps appearing | Reset store: `comet-cc daemon stop && rm -rf ~/.comet-cc/store.sqlite && comet-cc daemon start`. |
| Port 8443 / 8450 already in use | Another service has it. `COMET_CC_PROXY_PORT=18443 comet-cc daemon start` (and re-export before launching CC). Graph port is hard-coded at 8450 — kill whoever else uses it. |
| Graph shows few edges | Only nodes saved *after* the cross-link feature landed carry edges. Older nodes stay isolated until they happen to be cross-linked to a future neighbor. |
| `comet-cc install` regenerating CA every time | It's idempotent — if `ca.crt` + `server.pem` exist it reuses them. If something's off, inspect `~/.comet-cc/certs/`. |

## Testing

Layered tests, running from fastest to heaviest:

```bash
python tests/smoke.py --no-embed     # Pure-module regression, no LLM, no embedder
python tests/smoke.py                # + BGE-M3 embedder roundtrip
python tests/real_llm.py             # + claude -p sensor + compacter calls
python tests/daemon_multipass.py     # Buffer monotonicity across compactions
python tests/multiturn_live.py       # Full live CC session through the proxy
```

`multiturn_live.py` is the end-to-end check — drives CC through 7 turns
via `comet-cc run claude --resume <session_id>`, polls the daemon's
`queue_depth` until idle, then asserts the sensor queued, the compacter
saved at least one node, the rewrite fired, and the retrieval block
reached the outgoing request.

## Trust surface

The proxy sees plaintext of every message, both directions, plus your
Anthropic auth header. It only talks to `api.anthropic.com` (by default)
and only logs metadata. Everything is FOSS — audit `comet_cc/proxy/` if
you're routing sensitive work through it. No data leaves your machine
aside from the forwarded-as-is HTTPS request to Anthropic.

## Status

`0.1.0` — alpha, proxy architecture. Core pipeline verified end-to-end
(see `tests/multiturn_live.py`). Long-term drift, large-context edge
cases, and recovery after daemon restart (in-memory session state
currently resets) are not yet broadly dogfooded.

## License

[PolyForm Noncommercial 1.0.0](LICENSE) — free for personal, research,
and nonprofit use; commercial use requires a separate license from the
author.
