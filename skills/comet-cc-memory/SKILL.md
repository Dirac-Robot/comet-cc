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

### Read a specific node at one of three depths

```bash
comet-cc read-node <node_id>              # depth 0: summary only (cheapest)
comet-cc read-node <node_id> --depth 1    # detailed summary (LLM-generated on first call, cached)
comet-cc read-node <node_id> --depth 2    # raw turn data (exact verbatim text, no LLM)
comet-cc read-node <node_id> --links      # also list child nodes linked under this one
```

Pick the lowest depth that answers the question:

- **depth 0** is what retrieval already gave you (summary | trigger);
  re-reading it is free.
- **depth 1** when the summary glosses over specifics you need — numbers,
  names, decisions. First call runs a haiku LLM over the stored raw
  turns (~2-5s); subsequent calls return cached.
- **depth 2** when you need the exact words that were said — e.g., user's
  literal instruction, exact error message, exact code snippet. No LLM
  involved; dumps verbatim absorbed turns in order.

Escalate only as far as you need. Starting at depth 2 for a factual
question wastes tokens; staying at depth 0 when the user asked "what
exactly did I say" gives a paraphrase that misses the point.

`--links` is orthogonal to `--depth` and combines freely; use it when a
node is a bundle-parent or cluster head and you need to walk to its
children. The real flag is `--links` (plural, no "follow-"); there is
**no** `--raw`, `--follow-links`, or `--verbose` — use `--depth 2` and
`--links`.

### List every node in the current session

```bash
comet-cc list-session <session_id>           # parent nodes only
comet-cc list-session <session_id> --all     # include drill-down child nodes
```

Chronological timeline of the session's compacted nodes. Useful when the
user asks "what have we done so far" or you want to audit coverage.
`--all` adds bundle children (individual tool calls inside a bundle
parent) — usually noise unless you're inspecting a specific tool chain.

### Finding the current session id

`list-session` and `brief` both require a session id. You can get the
current session's id from any of:
- the `Current session: <sid>` line in the memory block injected at the
  top of the last user message;
- the `session=<sid>` field on retrieved nodes in that same block;
- `comet-cc search "<any query>"` — matching nodes print `session=<sid>`.

If none of those are present (fresh session, nothing compacted yet),
there is nothing to list or brief for — skip instead of guessing an id.

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
