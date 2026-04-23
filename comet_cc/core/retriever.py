"""Retriever — passive-first + active vector match.

Fills the context window for each turn: passive/both nodes always ride,
active nodes fill remaining slots by cosine similarity to the prompt.
"""

from __future__ import annotations

import numpy as np

from comet_cc import config
from comet_cc.core import vector
from comet_cc.core.store import NodeStore
from comet_cc.schemas import MemoryNode


def get_context_window(
    store: NodeStore,
    session_id: str | None,
    query: str | None = None,
    max_nodes: int = 8,
    min_score: float = 0.30,
) -> list[MemoryNode]:
    """Passive/both first, then active by vector match against `query`.

    Scoped to `session_id` unless `COMET_CC_CROSS_SESSION=1` is set.
    CoMeT-CC doesn't support session handoff; leaking another session's
    memory would be surprising, so session-scoped is the default.
    """
    cross = config.CROSS_SESSION_RETRIEVAL
    passives = store.list_passive(session_id=session_id, cross_session=cross)
    if len(passives) >= max_nodes:
        return passives[:max_nodes]

    remaining = max_nodes - len(passives)
    passive_ids = {n.node_id for n in passives}

    actives: list[MemoryNode] = []
    if query:
        candidates = store.list_active_with_embeddings(
            session_id=session_id, cross_session=cross,
        )
        candidates = [(n, e) for n, e in candidates if n.node_id not in passive_ids]
        if candidates:
            q_vec = vector.embed(query)
            pairs = [(n.node_id, e) for n, e in candidates]
            ranked = vector.cosine_search(q_vec, pairs, top_k=remaining, min_score=min_score)
            by_id = {n.node_id: n for n, _ in candidates}
            actives = [by_id[nid] for nid, _ in ranked]
    else:
        candidates = store.list_active_with_embeddings(
            session_id=session_id, cross_session=cross,
        )
        candidates_sorted = sorted(candidates, key=lambda ne: ne[0].created_at, reverse=True)
        actives = [n for n, _ in candidates_sorted
                   if n.node_id not in passive_ids][:remaining]

    return passives + actives


def render_nodes(nodes: list[MemoryNode]) -> str:
    """Render nodes in CoMeT's canonical single-line form:
        [node_id] (IMPORTANCE) summary | trigger
    Passive ordered before active (passive always rides; active is ranked)."""
    if not nodes:
        return ""
    lines = ["## Retrieved Memory"]
    for n in nodes:
        line = f"[{n.node_id}] ({n.importance}) {n.summary}"
        if n.trigger:
            line += f" | {n.trigger}"
        lines.append(line)
    return "\n".join(lines)


def render_session_brief(brief: str) -> str:
    """Render brief as a block. The brief already contains H2 section headers,
    so we wrap it in H1 instead to avoid duplicate ## headers."""
    if not brief.strip():
        return ""
    return "# Session Brief\n" + brief.strip()


_MEMORY_CLI_FOOTER = (
    "# Memory CLI (for active recall when the above missed something)\n"
    "- `comet-cc search \"<query>\" [--session <id>] [--top N]` — semantic "
    "search across all sessions\n"
    "- `comet-cc read-node <node_id>` — full details of a specific node\n"
    "- `comet-cc list-session <session_id>` — every compacted node in a session\n"
    "- `comet-cc brief <session_id>` — that session's live-rewritten brief\n"
    "Use when the user references prior work not visible in the retrieved "
    "memory above, or when a new task feels familiar."
)


def render_memory_cli_footer() -> str:
    return _MEMORY_CLI_FOOTER
