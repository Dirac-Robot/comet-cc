"""CoMeT-CC daemon — persistent process holding warm BGE-M3 + NodeStore +
background compact worker + TLS proxy. CC hits the proxy via
ANTHROPIC_BASE_URL; skill CLI hits the Unix socket for search/read-node.

Runs with a PID file. `comet-cc daemon start/stop/status` manage it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import socket
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path
from queue import Queue
from typing import Any

from loguru import logger

from comet_cc import config
from comet_cc.core import sensor, vector
from comet_cc.core.compacter import compact as run_compacter
from comet_cc.core.retriever import get_context_window, render_nodes, render_session_brief
from comet_cc.core.store import NodeStore
from comet_cc.parser import choose_policy_for_bundle, parse_transcript
from comet_cc.policies import ALL_POLICIES
from comet_cc.proxy.server import ProxyServer
from comet_cc.proxy.trim import TrimOrchestrator
from comet_cc.schemas import L1Memory, MemoryNode


class Daemon:
    def __init__(self):
        self.store = NodeStore(config.store_path())
        self._store_lock = threading.Lock()
        self.compact_queue: Queue[dict] = Queue()
        self._stop = threading.Event()
        # Per-session set of fingerprints already absorbed into a saved node.
        # Keeps buffer monotonic across compactions: [1..k] → node, next
        # run's buffer is [k+1..] only. In-memory → resets on daemon restart
        # (acceptable for PoC; promote to sqlite later if crash recovery matters).
        self._consumed_fp: dict[str, set[str]] = {}
        self._consumed_lock = threading.Lock()

        # Warm the embedder now — this is the whole reason the daemon exists.
        logger.info("preloading BGE-M3 embedder (~560MB)...")
        t0 = time.perf_counter()
        vector.embed("warmup")
        logger.info(f"embedder ready in {time.perf_counter() - t0:.2f}s")

        self._worker = threading.Thread(target=self._compact_loop,
                                        name="compact-worker", daemon=True)
        self._worker.start()

        # Trim orchestrator — drives sensor+compacter off the request path,
        # holds per-session summary state.
        self.trim = TrimOrchestrator(store=self.store, store_lock=self._store_lock)
        self._trim_worker = threading.Thread(
            target=self.trim.run_jobs, args=(self._stop,),
            name="trim-worker", daemon=True,
        )
        self._trim_worker.start()

        # Proxy lives in its own asyncio loop on a dedicated thread.
        self._proxy: ProxyServer | None = None
        self._proxy_thread = threading.Thread(
            target=self._proxy_run, name="proxy-loop", daemon=True,
        )
        self._proxy_thread.start()

    # ---------- RPC handlers ----------

    def handle(self, method: str, params: dict) -> dict:
        try:
            fn = getattr(self, f"_m_{method}", None)
            if fn is None:
                return {"ok": False, "error": f"unknown method {method}"}
            return fn(params)
        except Exception as e:
            logger.exception(f"handler {method} crashed: {e}")
            return {"ok": False, "error": str(e)}

    def _m_ping(self, _params) -> dict:
        return {
            "ok": True, "pong": True, "pid": os.getpid(),
            "proxy_host": config.PROXY_HOST, "proxy_port": config.PROXY_PORT,
        }

    def _m_get_context_window(self, p: dict) -> dict:
        session_id = p.get("session_id") or None
        with self._store_lock:
            nodes = get_context_window(
                self.store, session_id=session_id,
                query=p.get("query"),
                max_nodes=int(p.get("max_nodes", 8)),
                min_score=float(p.get("min_score", 0.30)),
            )
            brief = self.store.load_session_brief(session_id) if session_id else ""
        return {
            "ok": True,
            "nodes": [_node_to_dict(n) for n in nodes],
            "brief": brief,
        }

    def _m_save_compacted_node(self, p: dict) -> dict:
        node_dict = p["node"]
        emb_text = p.get("emb_text", "")
        session_brief = p.get("session_brief", "")
        node = _node_from_dict(node_dict)
        emb = vector.embed(emb_text) if emb_text else None
        with self._store_lock:
            self.store.save_node(node, embedding=emb)
            if session_brief.strip() and node.session_id:
                self.store.save_session_brief(node.session_id, session_brief.strip())
        return {"ok": True, "node_id": node.node_id}

    def _m_get_node(self, p: dict) -> dict:
        with self._store_lock:
            node = self.store.get_node(p["node_id"])
        if node is None:
            return {"ok": False, "error": "not found"}
        return {"ok": True, "node": _node_to_dict(node)}

    def _m_read_memory(self, p: dict) -> dict:
        """Tiered read: depth 0 = summary, 1 = detailed (lazy), 2 = raw turns."""
        nid = p["node_id"]
        depth = int(p.get("depth", 0))
        with self._store_lock:
            node = self.store.get_node(nid)
        if node is None:
            return {"ok": False, "error": "not found"}

        if depth == 0:
            text = (f"{node.summary} | {node.trigger}"
                    if node.trigger else node.summary)
            return {"ok": True, "depth": 0, "text": text,
                    "node": _node_to_dict(node)}

        if depth == 2:
            with self._store_lock:
                turns = self.store.get_raw_turns(nid)
            return {"ok": True, "depth": 2, "turns": turns,
                    "node": _node_to_dict(node)}

        # depth == 1: detailed_summary, lazy-generated + cached
        if node.detailed_summary:
            return {"ok": True, "depth": 1, "text": node.detailed_summary,
                    "cached": True, "node": _node_to_dict(node)}
        with self._store_lock:
            turns = self.store.get_raw_turns(nid)
        if not turns:
            return {"ok": True, "depth": 1, "text": node.summary,
                    "cached": False, "node": _node_to_dict(node),
                    "note": "no raw turns to expand from; fell back to summary"}
        raw_text = "\n\n".join(f"[{role}] {text}" for _, role, text in turns)
        from comet_cc.core.detail import generate_detailed_summary
        detailed = generate_detailed_summary(raw_text)
        if not detailed:
            return {"ok": True, "depth": 1, "text": node.summary,
                    "cached": False, "node": _node_to_dict(node),
                    "note": "detail generator returned empty; fell back to summary"}
        with self._store_lock:
            self.store.update_detailed_summary(nid, detailed)
        return {"ok": True, "depth": 1, "text": detailed, "cached": False,
                "node": _node_to_dict(node)}

    def _m_list_session_nodes(self, p: dict) -> dict:
        sid = p["session_id"]
        include_children = bool(p.get("include_children", False))
        with self._store_lock:
            nodes = self.store.list_session_nodes(
                sid, include_children=include_children,
            )
        return {"ok": True, "nodes": [_node_to_dict(n) for n in nodes]}

    def _m_list_linked_nodes(self, p: dict) -> dict:
        parent_id = p["parent_id"]
        with self._store_lock:
            nodes = self.store.list_linked_nodes(parent_id)
        return {"ok": True, "nodes": [_node_to_dict(n) for n in nodes]}

    def _m_list_all_nodes(self, _p: dict) -> dict:
        with self._store_lock:
            nodes = self.store.list_all()
        return {"ok": True, "nodes": [_node_to_dict(n) for n in nodes]}

    def _m_list_passive(self, p: dict) -> dict:
        with self._store_lock:
            nodes = self.store.list_passive(session_id=p.get("session_id"))
        return {"ok": True, "nodes": [_node_to_dict(n) for n in nodes]}

    def _m_load_session_brief(self, p: dict) -> dict:
        with self._store_lock:
            brief = self.store.load_session_brief(p["session_id"])
        return {"ok": True, "brief": brief}

    def _m_queue_compact(self, p: dict) -> dict:
        self.compact_queue.put({
            "session_id": p["session_id"],
            "transcript_path": p["transcript_path"],
        })
        return {"ok": True, "queued": True, "depth": self.compact_queue.qsize()}

    def _m_queue_depth(self, _p: dict) -> dict:
        """Reports pending jobs across both the legacy hook-era queue and
        the trim orchestrator. `active` counts the trim worker's in-flight
        compact across all sessions. Tests use this to await full idle."""
        trim_jobs = self.trim.jobs.qsize() if self.trim else 0
        trim_active = sum(
            1 for s in (self.trim.registry.snapshot() if self.trim else [])
            if s.compact_in_flight or s.sensor_queued
        )
        return {
            "ok": True,
            "queue_size": self.compact_queue.qsize() + trim_jobs,
            "active": self._active_job_count() + trim_active,
        }

    def _active_job_count(self) -> int:
        """1 if a hook-era compact job is mid-flight, 0 otherwise.
        (The trim worker's in-flight count is reported separately.)"""
        return 1 if getattr(self, "_worker_busy", False) else 0

    # ---------- compact worker ----------

    def _compact_loop(self) -> None:
        """Port of hooks/stop.py's synchronous flow, run in-process so the
        warm embedder + NodeStore are reused."""
        while not self._stop.is_set():
            try:
                job = self.compact_queue.get(timeout=1.0)
            except Exception:
                continue
            self._worker_busy = True
            try:
                self._do_compact(job)
            except Exception as e:
                logger.exception(f"compact job failed: {e}")
            finally:
                self._worker_busy = False
                self.compact_queue.task_done()

    def _do_compact(self, job: dict) -> None:
        session_id = job["session_id"]
        transcript_path = job["transcript_path"]
        logical = parse_transcript(transcript_path)
        if not logical:
            return

        with self._consumed_lock:
            consumed = set(self._consumed_fp.get(session_id, set()))

        buffer: list[L1Memory] = []
        last_node = None
        new_fps: list[str] = []
        for n in logical:
            fp = _fingerprint(n)
            if fp in consumed:
                continue  # already absorbed into an earlier node
            if fp in new_fps:
                continue  # dedupe within this pass
            new_fps.append(fp)
            buffer.append(L1Memory(content=n.content, raw_content=n.raw_content,
                                   entities=[fp]))
            last_node = n

        if not buffer or last_node is None:
            return

        load = sensor.assess_load(
            current_input=buffer[-1].raw_content[:4000],
            l1_buffer=buffer[:-1][-config.SENSOR_BUFFER_TAIL:],
            timeout=30,
        )
        reason = sensor.get_compaction_reason(
            load, buffer_size=len(buffer),
            max_l1_buffer=config.MAX_L1_BUFFER,
            min_l1_buffer=config.MIN_L1_BUFFER,
            load_threshold=config.LOAD_THRESHOLD,
        )
        logger.info(
            f"compact job: session={session_id} buffer={len(buffer)} "
            f"flow={load.logic_flow} load={load.load_level} reason={reason}"
        )
        if reason is None:
            return

        policy_name = (
            choose_policy_for_bundle(last_node)
            if last_node.kind == "tool_bundle" else "dialog"
        )
        policy = ALL_POLICIES.get(policy_name, ALL_POLICIES["dialog"])

        with self._store_lock:
            existing_tags = self.store.get_all_tags()
            existing_brief = self.store.load_session_brief(session_id)

        result = run_compacter(
            l1_buffer=buffer, policy=policy, session_id=session_id,
            compaction_reason=reason, existing_tags=existing_tags,
            existing_brief=existing_brief, timeout=180,
        )
        if result is None:
            logger.warning(f"compacter returned None for session={session_id}")
            return

        node, brief = result
        emb = vector.embed(f"{node.summary}\n{node.trigger}")
        with self._store_lock:
            self.store.save_node(node, embedding=emb)
            if brief and brief.strip():
                self.store.save_session_brief(session_id, brief.strip())
        # Mark these turns as consumed — next compact pass skips them and
        # the buffer resets to the turns that arrived after.
        with self._consumed_lock:
            self._consumed_fp.setdefault(session_id, set()).update(new_fps)
        logger.info(
            f"compact saved: node={node.node_id} policy={policy_name} "
            f"imp={node.importance} recall={node.recall_mode} "
            f"absorbed={len(new_fps)} turns"
        )

    # ---------- proxy thread ----------

    def _proxy_run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._proxy_coro())
        except Exception as e:
            logger.exception(f"proxy loop crashed: {e}")
        finally:
            loop.close()

    async def _proxy_coro(self) -> None:
        self._proxy = ProxyServer(rewrite=self.trim.rewrite)
        await self._proxy.start()
        while not self._stop.is_set():
            await asyncio.sleep(0.5)
        await self._proxy.stop()

    # ---------- server loop ----------

    def serve(self) -> None:
        sock_path = config.daemon_socket()
        if sock_path.exists():
            sock_path.unlink()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(sock_path))
        os.chmod(sock_path, 0o600)
        sock.listen(16)
        logger.info(f"listening on {sock_path}")

        try:
            while not self._stop.is_set():
                sock.settimeout(1.0)
                try:
                    conn, _ = sock.accept()
                except socket.timeout:
                    continue
                threading.Thread(
                    target=self._serve_conn, args=(conn,), daemon=True,
                ).start()
        finally:
            try:
                sock.close()
            finally:
                if sock_path.exists():
                    sock_path.unlink()

    def _serve_conn(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(60.0)
            buf = bytearray()
            while b"\n" not in buf:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
            if not buf:
                return
            line = buf.split(b"\n", 1)[0]
            req = json.loads(line.decode("utf-8"))
            resp = self.handle(req.get("method", ""), req.get("params") or {})
            conn.sendall(json.dumps(resp, ensure_ascii=False).encode("utf-8"))
        except Exception as e:
            logger.warning(f"conn error: {e}")
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def shutdown(self, *_a) -> None:
        logger.info("shutting down")
        self._stop.set()


def _fingerprint(node) -> str:
    base = "|".join(node.entry_uuids) or node.raw_content[:200]
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:16]


def _node_to_dict(n: MemoryNode) -> dict:
    return asdict(n)


def _node_from_dict(d: dict) -> MemoryNode:
    allowed = {f.name for f in MemoryNode.__dataclass_fields__.values()}
    return MemoryNode(**{k: v for k, v in d.items() if k in allowed})


def _setup_daemon_logging() -> None:
    logger.remove()
    logger.add(
        config.daemon_log(),
        rotation="10 MB", retention=3,
        format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}",
        level="INFO",
    )


def main() -> int:
    _setup_daemon_logging()
    pid_path = config.daemon_pid_file()
    pid_path.write_text(str(os.getpid()), encoding="utf-8")
    try:
        d = Daemon()
        signal.signal(signal.SIGTERM, d.shutdown)
        signal.signal(signal.SIGINT, d.shutdown)
        d.serve()
    finally:
        if pid_path.exists():
            try:
                pid_path.unlink()
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
