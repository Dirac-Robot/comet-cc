"""Shared hook utilities: payload parsing, logging setup, L1 buffer I/O."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path

from loguru import logger

from comet_cc import config
from comet_cc.schemas import L1Memory


def setup_logging(event_name: str) -> None:
    logger.remove()
    logger.add(
        config.log_path(),
        rotation="10 MB",
        retention=3,
        format="{time:YYYY-MM-DD HH:mm:ss} | [" + event_name + "] {message}",
        level="INFO",
    )


def read_payload() -> dict:
    try:
        return json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError as e:
        logger.error(f"hook payload parse failed: {e}")
        return {}


def emit_output(data: dict) -> None:
    """Write structured hook response to stdout as a single JSON object."""
    sys.stdout.write(json.dumps(data, ensure_ascii=False))
    sys.stdout.flush()


def load_l1(session_id: str) -> list[L1Memory]:
    path = config.l1_path(session_id)
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            d = json.loads(line)
            out.append(L1Memory(
                content=d.get("content", ""),
                raw_content=d.get("raw_content", ""),
                entities=d.get("entities", []),
                intent=d.get("intent"),
                timestamp=d.get("timestamp", 0),
            ))
        except json.JSONDecodeError:
            continue
    return out


def save_l1(session_id: str, buffer: list[L1Memory]) -> None:
    path = config.l1_path(session_id)
    lines = [json.dumps(asdict(m), ensure_ascii=False) for m in buffer]
    path.write_text("\n".join(lines), encoding="utf-8")


def clear_l1(session_id: str) -> None:
    path = config.l1_path(session_id)
    if path.exists():
        path.unlink()


def append_l1(session_id: str, new_items: list[L1Memory]) -> list[L1Memory]:
    buf = load_l1(session_id)
    buf.extend(new_items)
    save_l1(session_id, buf)
    return buf


def abort_ok() -> None:
    """Exit cleanly with no output — CC proceeds normally."""
    sys.exit(0)


def bail_if_internal() -> None:
    """Skip this hook if it was invoked inside a `claude -p` subprocess that
    the plugin itself spawned (sensor / compacter). Without this guard, the
    subprocess picks up project-scoped hooks and recursively re-enters the
    pipeline, polluting the store with the plugin's own internal chatter."""
    import os as _os
    if _os.environ.get("COMET_CC_INTERNAL"):
        sys.exit(0)
