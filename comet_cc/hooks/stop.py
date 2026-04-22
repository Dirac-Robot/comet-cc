"""Stop hook — queue compaction into the daemon (async, ~0.1s).

Daemon's background worker runs sensor + compacter + embedder using its
long-lived state. Falls back to in-process direct execution if the daemon
isn't running.
"""

from __future__ import annotations

import hashlib

from loguru import logger

from comet_cc import client, config
from comet_cc.core import sensor, vector
from comet_cc.core.compacter import compact as run_compacter
from comet_cc.core.store import NodeStore
from comet_cc.hooks._common import (
    abort_ok, append_l1, bail_if_internal, clear_l1, load_l1,
    read_payload, save_l1, setup_logging,
)
from comet_cc.parser import LogicalNode, choose_policy_for_bundle, parse_transcript
from comet_cc.policies import ALL_POLICIES
from comet_cc.schemas import L1Memory


def _fingerprint(node: LogicalNode) -> str:
    base = "|".join(node.entry_uuids) or node.raw_content[:200]
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:16]


def _direct(session_id: str, transcript_path: str) -> None:
    """Fallback path: sensor + compacter + embed + save all in this process.
    Used only when daemon isn't reachable. ~39s end-to-end."""
    logical = parse_transcript(transcript_path)
    if not logical:
        return

    existing = load_l1(session_id)
    seen = {m.entities[0] for m in existing if m.entities}

    new_items: list[tuple[LogicalNode, L1Memory]] = []
    for n in logical:
        fp = _fingerprint(n)
        if fp in seen:
            continue
        seen.add(fp)
        new_items.append((n, L1Memory(
            content=n.content, raw_content=n.raw_content, entities=[fp],
        )))
    if not new_items:
        return

    last_node, _ = new_items[-1]
    buf = append_l1(session_id, [m for _, m in new_items])
    load = sensor.assess_load(
        current_input=buf[-1].raw_content[:4000],
        l1_buffer=buf[:-1][-config.SENSOR_BUFFER_TAIL:],
        timeout=30,
    )
    reason = sensor.get_compaction_reason(
        load, buffer_size=len(buf),
        max_l1_buffer=config.MAX_L1_BUFFER,
        min_l1_buffer=config.MIN_L1_BUFFER,
        load_threshold=config.LOAD_THRESHOLD,
    )
    logger.info(f"direct sensor: flow={load.logic_flow} load={load.load_level} reason={reason}")
    if reason is None:
        return

    policy_name = (
        choose_policy_for_bundle(last_node)
        if last_node.kind == "tool_bundle" else "dialog"
    )
    policy = ALL_POLICIES.get(policy_name, ALL_POLICIES["dialog"])

    store = NodeStore(config.store_path())
    try:
        result = run_compacter(
            l1_buffer=buf, policy=policy, session_id=session_id,
            compaction_reason=reason,
            existing_tags=store.get_all_tags(),
            existing_brief=store.load_session_brief(session_id),
            timeout=180,
        )
        if result is None:
            return
        node, brief = result
        emb = vector.embed(f"{node.summary}\n{node.trigger}")
        store.save_node(node, embedding=emb)
        if brief and brief.strip():
            store.save_session_brief(session_id, brief.strip())
        logger.info(f"direct compact: node={node.node_id} policy={policy_name}")
    finally:
        store.close()
    clear_l1(session_id)
    save_l1(session_id, [])


def main() -> None:
    setup_logging("Stop"); bail_if_internal()
    payload = read_payload()
    session_id = payload.get("session_id") or ""
    transcript_path = payload.get("transcript_path") or ""

    if not (session_id and transcript_path):
        abort_ok()
        return

    resp = client.queue_compact(session_id, transcript_path, timeout=2.0)
    if resp and resp.get("ok"):
        logger.info(f"stop[rpc]: queued compact (depth={resp.get('depth')})")
        abort_ok()
        return

    logger.info("stop[direct]: daemon unreachable, running in-process")
    _direct(session_id, transcript_path)
    abort_ok()


if __name__ == "__main__":
    main()
