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
from comet_cc.core import retriever, sensor as sensor_mod
from comet_cc.core import vector
from comet_cc.core.compacter import compact as run_compacter
from comet_cc.core.store import NodeStore
from comet_cc.parser import LogicalNode, choose_policy_for_bundle
from comet_cc.policies import ALL_POLICIES
from comet_cc.proxy.extractor import (
    bundle_l1, extract_session_id, messages_to_l1, parse_messages_body,
)
from comet_cc.proxy.server import BlockedResponse
from comet_cc.proxy.session import SessionRegistry, SessionState
from comet_cc.schemas import L1Memory, MemoryNode


# Signature phrases unique to CC's native compactor prompt. If any of these
# are present, we assume CC is asking the model to /compact the session and
# we short-circuit with an error — CoMeT-CC already manages summarization,
# running CC's native compact on top would clobber our trim state.
_COMPACT_PROMPT_MARKERS = (
    "Your task is to create a detailed summary of this conversation",
    "Your task is to create a detailed summary of the conversation so far",
    "Your task is to create a detailed summary of the RECENT portion",
)


def _looks_like_native_compact(body_json: dict) -> bool:
    # Scan the last user message (where CC puts compact instructions) and,
    # for belt+suspenders, the system array.
    msgs = body_json.get("messages") or []
    for msg in reversed(msgs):
        if msg.get("role") != "user":
            continue
        c = msg.get("content")
        if isinstance(c, str):
            text = c
        elif isinstance(c, list):
            text = " ".join(
                b.get("text", "") for b in c
                if isinstance(b, dict) and b.get("type") == "text"
            )
        else:
            text = ""
        if any(m in text for m in _COMPACT_PROMPT_MARKERS):
            return True
        break
    sysarr = body_json.get("system") or []
    if isinstance(sysarr, list):
        for blk in sysarr:
            if isinstance(blk, dict) and any(
                m in blk.get("text", "") for m in _COMPACT_PROMPT_MARKERS
            ):
                return True
    return False


_COMPACT_BLOCKED_MSG = (
    "CoMeT-CC is managing this session's memory (see `comet-cc brief` / "
    "`comet-cc list-session`) and has intercepted the /compact request. "
    "The native compactor would clobber the plugin's trim state — keep "
    "chatting and the proxy will summarize in the background. Disable "
    "this guard by stopping the daemon (`comet-cc daemon stop`) if you "
    "actually want CC's native compact instead."
)


def _compact_blocked_body() -> bytes:
    payload = {
        "type": "error",
        "error": {
            "type": "invalid_request_error",
            "message": _COMPACT_BLOCKED_MSG,
        },
    }
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


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

    async def rewrite(self, method: str, path: str, body: bytes):
        if not path.startswith("/v1/messages") or not body:
            return body
        d = parse_messages_body(body)
        if d is None:
            return body

        # CC's native /compact would summarize the conversation in a format
        # that conflicts with our trim state. Refuse it at the proxy.
        if _looks_like_native_compact(d):
            sid_hint = extract_session_id(d) or "?"
            logger.info(f"blocked native /compact for session={sid_hint[:8]}")
            return BlockedResponse(
                status=400, body=_compact_blocked_body(),
                content_type="application/json",
            )

        sid = extract_session_id(d)
        if not sid:
            return body

        state = self.registry.get_or_create(sid)
        l1 = messages_to_l1(d)

        # Per-message absorption lookup (rewrite still acts on raw messages
        # to keep alternation intact with CC's original array).
        unabsorbed_indices = [
            i for i, e in enumerate(l1)
            if e.entities[0] not in state.absorbed_fps
        ]

        # Bundled view for sensor/compacter: tool_use + tool_result chains
        # collapse into a single logical L1 entry so a 5-step tool run
        # doesn't look like 10 turn-flow breaks to the sensor.
        msgs = d.get("messages") or []
        unabsorbed_msgs = [msgs[i] for i in unabsorbed_indices]
        bundled = bundle_l1({"messages": unabsorbed_msgs})

        # Throttled sensor dispatch — don't hammer the worker.
        now = time.monotonic()
        ready = (
            len(bundled) >= config.MIN_L1_BUFFER
            and not state.compact_in_flight
            and (now - state.last_sensor_check) >= SENSOR_THROTTLE_SEC
        )
        if ready:
            self.registry.touch_sensor(sid)
            self.jobs.put({
                "kind": "sensor_check",
                "session_id": sid,
                # Bundled buffer. Each entry's `fps` is a list — a bundle
                # carries every underlying message's fingerprint so the
                # post-compact absorption step can mask them all.
                "buffer": [(list(e.entities), e.content, e.raw_content)
                           for e in bundled],
            })
            logger.info(
                f"trim[{sid[:8]}]: queued sensor_check "
                f"bundled={len(bundled)} unabsorbed_msgs={len(unabsorbed_msgs)} total_msgs={len(l1)}"
            )

        mutated = False

        # ---- 1. Apply stored summary, if any ----
        if state.summary_user and state.summary_asst:
            # Keep messages whose fp isn't in absorbed_fps. CC always ends
            # a transcript with a user message on a new turn, so the tail
            # is a valid continuation after our injected pair.
            kept_idx = [
                i for i, e in enumerate(l1)
                if e.entities[0] not in state.absorbed_fps
            ]
            kept_msgs = [d["messages"][i] for i in kept_idx]
            summary_block = (
                "[PREVIOUS CONVERSATION SUMMARY — authoritative; the verbatim "
                "turns were trimmed to save context]\n\n" + state.summary_user
            )
            d["messages"] = [
                {"role": "user",
                 "content": [{"type": "text", "text": summary_block}]},
                {"role": "assistant",
                 "content": [{"type": "text", "text": state.summary_asst}]},
            ] + kept_msgs
            logger.info(
                f"trim[{sid[:8]}]: rewrote messages "
                f"{len(l1)} -> {len(d['messages'])} "
                f"(absorbed {len(state.absorbed_fps)})"
            )
            mutated = True

        # ---- 2. Retrieval injection ----
        # Prepend a `<system-reminder>`-style block to the last user message
        # with passive nodes + vector-matched actives + session brief. This
        # replaces the UserPromptSubmit hook's additionalContext path.
        try:
            retrieval_block = self._build_retrieval_block(sid, d)
        except Exception as e:
            logger.exception(f"retrieval failed for {sid[:8]}: {e}")
            retrieval_block = ""
        if retrieval_block:
            self._inject_into_last_user(d, retrieval_block)
            logger.info(
                f"trim[{sid[:8]}]: injected retrieval "
                f"({len(retrieval_block)} chars)"
            )
            mutated = True

        if not mutated:
            return body
        return json.dumps(d, ensure_ascii=False).encode("utf-8")

    # ---------- retrieval helpers ----------

    def _build_retrieval_block(self, session_id: str, body_json: dict) -> str:
        # Query = text of the last user message (what the model is about to answer)
        last_user_text = ""
        for m in reversed(body_json.get("messages", [])):
            if m.get("role") != "user":
                continue
            c = m.get("content")
            if isinstance(c, str):
                last_user_text = c
            elif isinstance(c, list):
                parts = [b.get("text", "") for b in c
                         if isinstance(b, dict) and b.get("type") == "text"]
                last_user_text = "\n".join(parts)
            break
        if not last_user_text.strip():
            return ""

        with self.store_lock:
            nodes = retriever.get_context_window(
                self.store, session_id=session_id,
                query=last_user_text,
                max_nodes=config.MAX_CONTEXT_NODES,
                min_score=config.MIN_SIMILARITY,
            )
            brief = self.store.load_session_brief(session_id)

        parts = []
        brief_block = retriever.render_session_brief(brief)
        if brief_block:
            parts.append(brief_block)
        node_block = retriever.render_nodes(nodes)
        if node_block:
            parts.append(node_block)
        parts.append(retriever.render_memory_cli_footer())
        body = "\n\n".join(parts).strip()
        if not body:
            return ""
        return (
            "<system-reminder>\n"
            "Persistent memory context injected by CoMeT-CC "
            "(cross-session + session brief):\n\n"
            f"{body}\n"
            "</system-reminder>"
        )

    @staticmethod
    def _inject_into_last_user(body_json: dict, block: str) -> None:
        msgs = body_json.get("messages", [])
        for i in range(len(msgs) - 1, -1, -1):
            m = msgs[i]
            if m.get("role") != "user":
                continue
            c = m.get("content")
            if isinstance(c, str):
                m["content"] = [
                    {"type": "text", "text": block},
                    {"type": "text", "text": c},
                ]
            elif isinstance(c, list):
                m["content"] = [{"type": "text", "text": block}, *c]
            break

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
        # Each tuple is (fps_list, content_preview, raw_content). fps_list
        # has N entries for an N-message bundle, 1 entry for plain turns.
        buf_raw: list[tuple[list[str], str, str]] = job["buffer"]
        if len(buf_raw) < config.MIN_L1_BUFFER:
            return

        buffer = [
            L1Memory(content=c, raw_content=rc, entities=list(fps))
            for fps, c, rc in buf_raw
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
            # Derive tier-3 turns from the buffer. content starts with
            # "[role] ..." (see extractor._text_of); split once to recover
            # the role for each absorbed turn.
            tier3: list[tuple[str, str]] = []
            for mem in buffer:
                role = "user"
                if mem.content.startswith("[") and "] " in mem.content:
                    role = mem.content.split("] ", 1)[0].lstrip("[")
                tier3.append((role, mem.raw_content))
            with self.store_lock:
                self.store.save_node(node, embedding=emb)
                if tier3:
                    self.store.save_raw_turns(node.node_id, tier3)
                if brief and brief.strip():
                    self.store.save_session_brief(sid, brief.strip())

            # Flatten bundle fingerprints — each underlying message must
            # be marked absorbed individually so the rewrite path can mask
            # them out one by one.
            absorbed = set()
            for fps, _, _ in buf_raw:
                absorbed.update(fps)
            self.registry.mark_compact_done(
                session_id=sid,
                absorbed_fps=absorbed,
                summary_user=node.summary,
                summary_asst=_synth_ack(node.summary),
            )
            logger.info(
                f"compact[{sid[:8]}] saved node={node.node_id} "
                f"imp={node.importance} bundled={len(buf_raw)} "
                f"absorbed_msgs={len(absorbed)}"
            )
        finally:
            # Ensures compact_in_flight flips back even if we returned early.
            state = self.registry.get(sid)
            if state:
                state.compact_in_flight = False
