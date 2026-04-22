# CoMeT-CC

CoMeT algorithm as a native Claude Code plugin. Sensor + compacter + retriever run inside CC via hooks + `claude -p` subprocess calls — no external API keys, no CoMeT package imports.

## What it does

A shadow memory system rides alongside every Claude Code session:

1. **Sensor** (haiku, per turn) assesses each turn's cognitive load, topic continuity, redundancy.
2. **Compacter** (sonnet, on sensor trip) structures the L1 buffer into a MemoryNode — summary, trigger, recall_mode, importance, topic_tags — plus an optional session brief.
3. **Store** (sqlite + BGE-M3 embeddings) persists nodes across sessions with passive/active/both recall semantics.
4. **Retriever** (every user prompt) injects passive nodes + active matches + session brief via `additionalContext`.
5. **PreCompact** inject the store's pre-digested summaries so CC's native compaction anchors on them instead of re-summarizing from scratch.

## Install

```bash
pip install -e .
comet-cc install
```

`comet-cc install` registers four hooks in `~/.claude/settings.json`:

| Event             | Role                                                    |
|-------------------|---------------------------------------------------------|
| SessionStart      | Ensure store + L1 buffer exist                          |
| UserPromptSubmit  | Retrieve passive + active matches → additionalContext   |
| Stop              | Sensor pass; compact on trip; persist L2 node + brief   |
| PreCompact        | Surface accumulated store to anchor CC's compaction     |

## Tuning

| Env var                       | Default | Role                                   |
|-------------------------------|---------|----------------------------------------|
| `COMET_CC_HOME`               | `~/.comet-cc` | Store + L1 + logs root             |
| `COMET_CC_MAX_L1`             | 20      | Hard cap on L1 buffer (buffer_overflow)|
| `COMET_CC_MIN_L1`             | 3       | Below this, never compact              |
| `COMET_CC_LOAD_THRESHOLD`     | 4       | Sensor load level that trips compact   |
| `COMET_CC_MAX_CONTEXT_NODES`  | 8       | Retriever injection cap per turn       |
| `COMET_CC_MIN_SIM`            | 0.30    | Vector match floor for active nodes    |

## Provenance

- Sensor & compacter prompts are ported verbatim from [CoMeT](https://github.com/…/CoMeT): `templates/cognitive_load.txt`, `templates/compacting_base.txt`.
- Policy blocks (DIALOG / ARTIFACT_CODE / EXECUTION_TRACE / ...) are verbatim from [CoBrA](https://github.com/…/CoBrA) `backend/services/memory_policy.py`.
- Architecture is CoMeT's L1→L2 + passive/active/both, minus consolidation and lessons (post-session batch work — out of scope for a CC plugin).

## Status

0.0.1 alpha — PoC skeleton. Unit tests not yet written.
