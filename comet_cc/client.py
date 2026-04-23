"""Unix socket RPC client. Hooks call these helpers; each returns `None`
on connection failure so the caller can fall back to direct in-process
execution if the daemon isn't running."""

from __future__ import annotations

import json
import socket
from typing import Any

from loguru import logger

from comet_cc import config


def _rpc(method: str, timeout: float, **params) -> dict | None:
    sock_path = config.daemon_socket()
    if not sock_path.exists():
        return None
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(str(sock_path))
            req = json.dumps({"method": method, "params": params}, ensure_ascii=False)
            s.sendall(req.encode("utf-8") + b"\n")
            s.shutdown(socket.SHUT_WR)
            buf = bytearray()
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
        if not buf:
            return None
        return json.loads(buf.decode("utf-8"))
    except (FileNotFoundError, ConnectionRefusedError, socket.timeout, OSError) as e:
        logger.debug(f"rpc {method} failed: {e}")
        return None
    except json.JSONDecodeError as e:
        logger.warning(f"rpc {method} invalid response: {e}")
        return None


def ping(timeout: float = 1.0) -> bool:
    r = _rpc("ping", timeout)
    return bool(r and r.get("ok"))


def get_context_window(session_id: str, query: str | None,
                       max_nodes: int, min_score: float,
                       timeout: float = 10.0) -> dict | None:
    return _rpc("get_context_window", timeout,
                session_id=session_id, query=query,
                max_nodes=max_nodes, min_score=min_score)


def save_compacted_node(node_dict: dict, emb_text: str,
                        session_brief: str = "",
                        timeout: float = 10.0) -> dict | None:
    return _rpc("save_compacted_node", timeout,
                node=node_dict, emb_text=emb_text,
                session_brief=session_brief)


def get_node(node_id: str, timeout: float = 5.0) -> dict | None:
    return _rpc("get_node", timeout, node_id=node_id)


def read_memory(node_id: str, depth: int = 0, timeout: float = 120.0) -> dict | None:
    """Tiered read. depth 1 may block up to ~60s on LLM cold path."""
    return _rpc("read_memory", timeout, node_id=node_id, depth=depth)


def list_session_nodes(session_id: str, include_children: bool = False,
                       timeout: float = 5.0) -> dict | None:
    return _rpc("list_session_nodes", timeout,
                session_id=session_id, include_children=include_children)


def list_linked_nodes(parent_id: str, timeout: float = 5.0) -> dict | None:
    return _rpc("list_linked_nodes", timeout, parent_id=parent_id)


def list_all_nodes(timeout: float = 10.0) -> dict | None:
    return _rpc("list_all_nodes", timeout)


def list_passive(session_id: str | None, timeout: float = 5.0) -> dict | None:
    return _rpc("list_passive", timeout, session_id=session_id)


def load_session_brief(session_id: str, timeout: float = 5.0) -> dict | None:
    return _rpc("load_session_brief", timeout, session_id=session_id)


def queue_compact(session_id: str, transcript_path: str,
                  timeout: float = 2.0) -> dict | None:
    """Fire-and-forget: daemon queues a compact job in its worker thread."""
    return _rpc("queue_compact", timeout,
                session_id=session_id, transcript_path=transcript_path)


def queue_depth(timeout: float = 2.0) -> dict | None:
    """Ask the daemon how many compact jobs are pending + whether one is
    currently mid-flight. Tests use this to wait for a quiescent state."""
    return _rpc("queue_depth", timeout)
