# CoMeT-CC

Persistent memory layer for Claude Code. Runs as a local TLS proxy that CC
routes through via `ANTHROPIC_BASE_URL`; every `/v1/messages` request is
inspected, optionally rewritten (raw turns replaced with a running
summary), and forwarded to Anthropic. Drives summarization with `claude -p`
subprocesses so there are no external API keys to manage.

## What it does

CC sessions are ephemeral. When the context window fills, CC's built-in
compactor compresses older turns into a short summary and anything older
effectively disappears. CoMeT-CC rides alongside each session:

- A **haiku sensor** inspects the outgoing request per turn for topic
  shifts, cognitive load, and buffer size.
- On trip, a **sonnet compacter** produces a structured `MemoryNode`
  (summary, trigger, tags, importance, recall mode) plus a rolling
  session brief.
- Nodes land in a local **sqlite store** with BGE-M3 multilingual
  embeddings, indexed across every session on the machine.
- Every outgoing request gets:
  - **Summary rewrite**: absorbed turns are replaced with a `(user,
    assistant)` summary pair, keeping the tail intact. CC's local
    transcript stays untouched; only what Anthropic processes shrinks.
  - **Retrieval injection**: passive/both nodes plus vector-matched
    active nodes plus the session brief ride as a `<system-reminder>`
    block on the last user message.
- A **skill** (`comet-cc-memory`) + CLI (`search / read-node /
  list-session / brief`) give the model an active-recall path when the
  automatic injection misses something.

A long-lived **daemon** hosts the warm BGE-M3 embedder, the NodeStore, a
background sensor/compacter worker, a Unix-socket RPC endpoint for the
skill CLI, and the TLS proxy itself — all in one process.

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
comet-cc install           # CA + skill
comet-cc uninstall         # remove skill (cert + store kept)
comet-cc status            # cert, skill, daemon, proxy state
comet-cc env               # print ANTHROPIC_BASE_URL + NODE_EXTRA_CA_CERTS

comet-cc daemon start | stop | status
comet-cc run <cmd> [args...]

# Memory operations (also invoked by the skill from inside CC)
comet-cc search "<query>" [--session <id>] [--top N]
comet-cc read-node <node_id>
comet-cc list-session <session_id>
comet-cc brief <session_id>
```

## How it compares to the full CoBrA harness

CoMeT-CC is a lightweight variant. One concession remains — the full
harness's end-to-end consolidation pipeline is out of scope here.

### Lightweight memory harness

The full harness covers many modalities (dialog / code / execution
traces / external APIs / images / session handoffs) and runs a
post-session consolidation stage (dedup, cross-session link graph,
cluster synthesis, lessons extraction). CoMeT-CC keeps only what a CC
session benefits from day-to-day: two modalities (dialog / code),
passive/active/both recall modes, summary/trigger semantics, vector
search. Batch consolidation is deliberately out of scope.

If that matters for your workflow, use the full harness directly.
CoMeT-CC is for bolting a persistent memory layer onto an otherwise
native Claude Code workflow.

### Why a proxy instead of hooks

An earlier version of CoMeT-CC used CC's hook system (SessionStart /
UserPromptSubmit / Stop / PreCompact). That path had two structural
limits: (1) raw turns could only be replaced at CC's own compact
boundary, because hooks can augment but not rewrite messages in flight;
(2) anchors went in *alongside* CC's compactor rather than replacing
its output. The proxy architecture lifts both — every outgoing request
passes through our listener, so a summary pair can substitute absorbed
turns at any turn, and CC's native compactor effectively never has to
run (the upstream model only ever sees the compacted view). The hook
version is archived on the `hook-arch-archive` branch.

## Configuration

| Env var                       | Default          | Role                                        |
|-------------------------------|------------------|---------------------------------------------|
| `COMET_CC_HOME`               | `~/.comet-cc`    | Store + certs + logs root                   |
| `COMET_CC_PROXY_HOST`         | `127.0.0.1`      | Bind address for the TLS listener           |
| `COMET_CC_PROXY_PORT`         | `8443`           | Port for the TLS listener                   |
| `COMET_CC_UPSTREAM`           | `https://api.anthropic.com` | Where the proxy forwards          |
| `COMET_CC_MAX_L1`             | `20`             | Hard cap on unabsorbed buffer turns         |
| `COMET_CC_MIN_L1`             | `3`              | Minimum buffer before compaction considered |
| `COMET_CC_LOAD_THRESHOLD`     | `4`              | Sensor load (1–5) that trips compaction     |
| `COMET_CC_MAX_CONTEXT_NODES`  | `8`              | Retrieval cap injected per turn             |
| `COMET_CC_MIN_SIM`            | `0.30`           | Cosine floor for active-node matching       |

## Troubleshooting

| Symptom | Fix |
|---|---|
| `certificate verify failed` from CC | `NODE_EXTRA_CA_CERTS` not propagated. Use `comet-cc run claude …` or `eval "$(comet-cc env)"`. |
| `connection refused` | Daemon isn't up. `comet-cc daemon status`; if down, `comet-cc daemon start`. |
| Trim never fires on your expected turn | Haiku sensor is non-deterministic on short/related topics. Lower `COMET_CC_MAX_L1` to force the overflow path, or watch `~/.comet-cc/logs/daemon.log` to see what it judged. |
| Stale/unwanted summary keeps appearing | Reset store: `comet-cc daemon stop && rm -rf ~/.comet-cc/store.sqlite && comet-cc daemon start`. |
| Port 8443 already in use | Another service has it. Override: `COMET_CC_PROXY_PORT=18443 comet-cc daemon start` and re-export before launching CC. |
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
