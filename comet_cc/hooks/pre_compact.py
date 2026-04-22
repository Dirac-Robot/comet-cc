"""PreCompact hook — surface accumulated store summaries to CC's compactor.

Tries RPC first (warm store in daemon). Falls back to direct NodeStore open.
Work is purely read-only + tiny, so latency is <100ms in both paths.
"""

from __future__ import annotations

from loguru import logger

from comet_cc import client, config
from comet_cc.core import retriever
from comet_cc.core.store import NodeStore
from comet_cc.hooks._common import bail_if_internal,  abort_ok, emit_output, read_payload, setup_logging
from comet_cc.schemas import MemoryNode


def _nodes_from_rpc(payload: list[dict]) -> list[MemoryNode]:
    allowed = {f.name for f in MemoryNode.__dataclass_fields__.values()}
    return [
        MemoryNode(**{k: v for k, v in d.items() if k in allowed})
        for d in payload
    ]


def _gather_via_rpc(session_id: str) -> tuple[list[MemoryNode], str] | None:
    sess_resp = client.list_session_nodes(session_id) if session_id else None
    passive_resp = client.list_passive(session_id)
    brief_resp = client.load_session_brief(session_id) if session_id else None
    if not passive_resp or not passive_resp.get("ok"):
        return None
    session_nodes = (
        _nodes_from_rpc(sess_resp["nodes"]) if sess_resp and sess_resp.get("ok") else []
    )
    passive_nodes = _nodes_from_rpc(passive_resp["nodes"])
    brief = (brief_resp or {}).get("brief", "") if brief_resp else ""
    merged = {n.node_id: n for n in passive_nodes}
    for n in session_nodes:
        merged.setdefault(n.node_id, n)
    return list(merged.values()), brief


def _gather_direct(session_id: str) -> tuple[list[MemoryNode], str]:
    store = NodeStore(config.store_path())
    try:
        session_nodes = store.list_session_nodes(session_id) if session_id else []
        passive = store.list_passive(session_id=session_id)
        brief = store.load_session_brief(session_id) if session_id else ""
    finally:
        store.close()
    merged = {n.node_id: n for n in passive}
    for n in session_nodes:
        merged.setdefault(n.node_id, n)
    return list(merged.values()), brief


def main() -> None:
    setup_logging("PreCompact"); bail_if_internal()
    payload = read_payload()
    session_id = payload.get("session_id") or ""
    trigger = payload.get("trigger", "auto")

    rpc_result = _gather_via_rpc(session_id)
    if rpc_result is not None:
        nodes, brief = rpc_result
        source = "rpc"
    else:
        nodes, brief = _gather_direct(session_id)
        source = "direct"

    if not nodes and not brief.strip():
        abort_ok()
        return

    parts = [
        "The following pre-digested memory has already been indexed by the "
        "CoMeT-CC plugin during this session. Use it as the anchor for "
        "compaction — do NOT re-summarize items already captured here, and "
        "preserve their summary/trigger semantics in your output.",
    ]
    brief_block = retriever.render_session_brief(brief)
    if brief_block:
        parts.append(brief_block)
    nodes_block = retriever.render_nodes(nodes)
    if nodes_block:
        parts.append(nodes_block)

    context = "\n\n".join(parts)
    logger.info(
        f"pre_compact[{source}]: session={session_id} trigger={trigger} "
        f"nodes={len(nodes)} brief={bool(brief)}"
    )
    # PreCompact schema rejects hookSpecificOutput — only top-level fields are
    # accepted. `systemMessage` rides into CC's compactor prompt as a system
    # directive, which is exactly where we want our anchors.
    emit_output({"systemMessage": context})


if __name__ == "__main__":
    main()
