"""Trim orchestrator — the heart of the proxy's value.

Flow per /v1/messages request:
  1. Parse body; extract session_id.
  2. Convert messages to L1 fingerprints.
  3. If session has a stored summary pair: drop absorbed messages from the
     outgoing request and inject (user_summary, assistant_ack) in their place.
  4. Throttled + deduped: if enough unabsorbed messages have accumulated,
     enqueue a sensor-check job on the daemon's compact worker. The job
     may then trigger a compacter run which updates session state for
     future requests. LLM subprocesses run OFF the request path.

The rewrite function returned is what ProxyServer installs as its hook.
"""
from __future__ import annotations

import json
import time
from queue import Queue
from typing import Awaitable, Callable

from loguru import logger

from comet_cc import config
from comet_cc.core import sensor as sensor_mod
from comet_cc.core import vector
from comet_cc.core.compacter import compact as run_compacter
from comet_cc.core.store import NodeStore
from comet_cc.parser import LogicalNode, choose_policy_for_bundle
from comet_cc.policies import ALL_POLICIES
from comet_cc.proxy.extractor import (
    extract_session_id, messages_to_l1, parse_messages_body,
)
from comet_cc.proxy.session import SessionRegistry, SessionState
from comet_cc.schemas import L1Memory, MemoryNode


# Throttle: only consider enqueuing a sensor job once per this many seconds
# per session. Subsequent requests within the window skip the check.
SENSOR_THROTTLE_SEC = 15.0


def _synth_ack(summary_text: str) -> str:
    """Tiny fixed acknowledgment from the fake 'assistant' turn that pairs
    the summary. Keeps alternation intact without hallucinating content."""
    return (
        "Understood — I have the prior context above. Ready for the next "
        "instruction."
    )


class TrimOrchestrator:
    """Holds per-session state + the background job queue. Exposes:
      - rewrite() → aiohttp hook (async, lives on the request path)
      - run_jobs() → to be driven by the daemon's worker thread
    """

    def __init__(
        self,
        store: NodeStore,
        store_lock,  # threading.Lock protecting `store`
    ) -> None:
        self.store = store
        self.store_lock = store_lock
        self.registry = SessionRegistry()
        self.jobs: Queue[dict] = Queue()

    # ---------- request-path: fast, no LLM ----------

    async def rewrite(self, method: str, path: str, body: bytes) -> bytes:
        if not path.startswith("/v1/messages") or not body:
            return body
        d = parse_messages_body(body)
        if d is None:
            return body
        sid = extract_session_id(d)
        if not sid:
            return body

        state = self.registry.get_or_create(sid)
        l1 = messages_to_l1(d)
        unabsorbed = [e for e in l1 if e.entities[0] not in state.absorbed_fps]

        # Throttled sensor dispatch — don't hammer the worker.
        now = time.monotonic()
        ready = (
            len(unabsorbed) >= config.MIN_L1_BUFFER
            and not state.compact_in_flight
            and (now - state.last_sensor_check) >= SENSOR_THROTTLE_SEC
        )
        if ready:
            self.registry.touch_sensor(sid)
            self.jobs.put({
                "kind": "sensor_check",
                "session_id": sid,
                # Snapshot of buffer fps + raw contents for the worker
                "buffer": [(e.entities[0], e.content, e.raw_content)
                           for e in unabsorbed],
                "all_fps": [e.entities[0] for e in l1],
            })
            logger.info(
                f"trim[{sid[:8]}]: queued sensor_check "
                f"unabsorbed={len(unabsorbed)} total={len(l1)}"
            )

        # Apply stored summary to this request, if we have one.
        if not state.summary_user or not state.summary_asst:
            return body

        # Keep only messages whose fp isn't in absorbed_fps. CC always ends a
        # transcript with a user message on a new turn, so the tail is a
        # valid continuation after our injected pair.
        kept_idx = [
            i for i, e in enumerate(l1)
            if e.entities[0] not in state.absorbed_fps
        ]
        kept_msgs = [d["messages"][i] for i in kept_idx]

        summary_block = (
            "[PREVIOUS CONVERSATION SUMMARY — authoritative; the verbatim "
            "turns were trimmed to save context]\n\n" + state.summary_user
        )
        new_msgs = [
            {"role": "user",
             "content": [{"type": "text", "text": summary_block}]},
            {"role": "assistant",
             "content": [{"type": "text", "text": state.summary_asst}]},
        ] + kept_msgs
        d["messages"] = new_msgs
        logger.info(
            f"trim[{sid[:8]}]: rewrote messages "
            f"{len(l1)} -> {len(new_msgs)} (absorbed {len(state.absorbed_fps)})"
        )
        return json.dumps(d, ensure_ascii=False).encode("utf-8")

    # ---------- worker-path: slow, LLM subprocesses ----------

    def run_jobs(self, stop_event) -> None:
        """Drained by a daemon worker thread. Blocks on self.jobs."""
        while not stop_event.is_set():
            try:
                job = self.jobs.get(timeout=1.0)
            except Exception:
                continue
            try:
                if job["kind"] == "sensor_check":
                    self._do_sensor_check(job)
            except Exception as e:
                logger.exception(f"trim job crashed: {e}")
            finally:
                self.jobs.task_done()

    def _do_sensor_check(self, job: dict) -> None:
        sid: str = job["session_id"]
        buf_raw: list[tuple[str, str, str]] = job["buffer"]
        if len(buf_raw) < config.MIN_L1_BUFFER:
            return

        # Rebuild L1Memory list (we stripped dataclass in queue payload).
        buffer = [
            L1Memory(content=c, raw_content=rc, entities=[fp])
            for fp, c, rc in buf_raw
        ]

        self.registry.mark_compact_start(sid)
        try:
            load = sensor_mod.assess_load(
                current_input=buffer[-1].raw_content[:4000],
                l1_buffer=buffer[:-1][-config.SENSOR_BUFFER_TAIL:],
                timeout=30,
            )
            reason = sensor_mod.get_compaction_reason(
                load,
                buffer_size=len(buffer),
                max_l1_buffer=config.MAX_L1_BUFFER,
                min_l1_buffer=config.MIN_L1_BUFFER,
                load_threshold=config.LOAD_THRESHOLD,
            )
            logger.info(
                f"sensor[{sid[:8]}]: flow={load.logic_flow} load={load.load_level} "
                f"buf={len(buffer)} reason={reason}"
            )
            if reason is None:
                return

            with self.store_lock:
                existing_tags = self.store.get_all_tags()
                existing_brief = self.store.load_session_brief(sid)

            policy = ALL_POLICIES["dialog"]
            result = run_compacter(
                l1_buffer=buffer,
                policy=policy,
                session_id=sid,
                compaction_reason=reason,
                existing_tags=existing_tags,
                existing_brief=existing_brief,
                timeout=180,
            )
            if result is None:
                logger.warning(f"compacter[{sid[:8]}] returned None")
                return

            node, brief = result
            emb = vector.embed(f"{node.summary}\n{node.trigger}")
            with self.store_lock:
                self.store.save_node(node, embedding=emb)
                if brief and brief.strip():
                    self.store.save_session_brief(sid, brief.strip())

            self.registry.mark_compact_done(
                session_id=sid,
                absorbed_fps={fp for fp, _, _ in buf_raw},
                summary_user=node.summary,
                summary_asst=_synth_ack(node.summary),
            )
            logger.info(
                f"compact[{sid[:8]}] saved node={node.node_id} "
                f"imp={node.importance} absorbed={len(buf_raw)} turns"
            )
        finally:
            # Ensures compact_in_flight flips back even if we returned early.
            state = self.registry.get(sid)
            if state:
                state.compact_in_flight = False
