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
    """Passive/both first, then active by vector match against `query`,
    then 1-hop graph expansion from the cosine-top via `links`.

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
    ranked_scores: list[tuple[str, float]] = []
    if query:
        candidates = store.list_active_with_embeddings(
            session_id=session_id, cross_session=cross,
        )
        candidates = [(n, e) for n, e in candidates if n.node_id not in passive_ids]
        if candidates:
            q_vec = vector.embed(query)
            pairs = [(n.node_id, e) for n, e in candidates]
            ranked_scores = vector.cosine_search(
                q_vec, pairs, top_k=remaining, min_score=min_score,
            )
            by_id = {n.node_id: n for n, _ in candidates}
            actives = [by_id[nid] for nid, _ in ranked_scores]
    else:
        candidates = store.list_active_with_embeddings(
            session_id=session_id, cross_session=cross,
        )
        candidates_sorted = sorted(candidates, key=lambda ne: ne[0].created_at, reverse=True)
        actives = [n for n, _ in candidates_sorted
                   if n.node_id not in passive_ids][:remaining]

    primary = passives + actives

    # 1-hop expansion via `links`. Neighbor nodes ride in at a decayed
    # relevance (hop1_decay × parent's similarity) so they don't displace
    # the direct matches. Bounded by max_nodes; skips passives/duplicates.
    if config.HOP1_DECAY > 0 and ranked_scores:
        primary_ids = {n.node_id for n in primary}
        neighbor_scores: dict[str, float] = {}
        id_to_node = {n.node_id: n for n in actives}
        for nid, sim in ranked_scores:
            node = id_to_node.get(nid)
            if node is None or not node.links:
                continue
            base = sim * config.HOP1_DECAY
            for link_id in node.links:
                if link_id in primary_ids:
                    continue
                # keep the strongest hop-in score for a given neighbor
                if neighbor_scores.get(link_id, 0.0) < base:
                    neighbor_scores[link_id] = base
        if neighbor_scores and len(primary) < max_nodes:
            sorted_neighbors = sorted(
                neighbor_scores.items(), key=lambda kv: kv[1], reverse=True,
            )
            slots = max_nodes - len(primary)
            fetched = store.get_nodes([nid for nid, _ in sorted_neighbors[:slots]])
            # Drop child nodes surfaced via the hop (shouldn't happen
            # structurally, but keeps the memory map parent-only).
            primary += [n for n in fetched if n.parent_node_id is None]

    return primary[:max_nodes]


def render_nodes(nodes: list[MemoryNode]) -> str:
    """Render nodes in CoMeT's canonical single-line form:
        [node_id] (IMPORTANCE) summary | trigger
    Passive ordered before active (passive always rides; active is ranked)."""
    if not nodes:
        return ""
    lines = ["## Retrieved Memory"]
    for n in nodes:
        # MED is the implicit default — elide it so only HIGH/LOW stand out.
        imp = "" if n.importance == "MED" else f" ({n.importance})"
        line = f"[{n.node_id}]{imp} {n.summary}"
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
