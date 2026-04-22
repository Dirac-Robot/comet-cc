"""UserPromptSubmit hook — inject retrieved memory as additionalContext.

Prefers the daemon (warm BGE-M3 → <100ms). Falls back to direct in-process
retrieval (~12s cold) if the daemon isn't running.
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


def main() -> None:
    setup_logging("UserPromptSubmit"); bail_if_internal()
    payload = read_payload()
    session_id = payload.get("session_id") or ""
    prompt = payload.get("prompt") or ""

    if not prompt.strip():
        abort_ok()
        return

    resp = client.get_context_window(
        session_id=session_id, query=prompt,
        max_nodes=config.MAX_CONTEXT_NODES,
        min_score=config.MIN_SIMILARITY,
        timeout=10.0,
    )

    if resp and resp.get("ok"):
        nodes = _nodes_from_rpc(resp.get("nodes", []))
        brief = resp.get("brief", "")
        source = "rpc"
    else:
        store = NodeStore(config.store_path())
        try:
            nodes = retriever.get_context_window(
                store, session_id=session_id, query=prompt,
                max_nodes=config.MAX_CONTEXT_NODES,
                min_score=config.MIN_SIMILARITY,
            )
            brief = store.load_session_brief(session_id)
        finally:
            store.close()
        source = "direct"

    parts = []
    brief_block = retriever.render_session_brief(brief)
    if brief_block:
        parts.append(brief_block)
    nodes_block = retriever.render_nodes(nodes)
    if nodes_block:
        parts.append(nodes_block)
    # The memory-CLI footer always rides — tells Claude that active recall
    # is available when passive injection above is insufficient.
    parts.append(retriever.render_memory_cli_footer())

    context = "\n\n".join(parts)
    logger.info(
        f"user_prompt[{source}]: session={session_id} "
        f"nodes={len(nodes)} brief={bool(brief)} ctx_chars={len(context)}"
    )
    emit_output({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context,
        },
    })


if __name__ == "__main__":
    main()
