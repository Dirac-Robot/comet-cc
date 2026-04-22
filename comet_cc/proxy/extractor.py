"""Parse an outgoing /v1/messages request body into something the sensor
can reason about: session id + a list of L1Memory entries (one per message).

CC resends its full local transcript on every turn, so fingerprinting by
content hash is enough to tell "what's new since last request".
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from comet_cc.schemas import L1Memory


def _fingerprint(role: str, text: str) -> str:
    return hashlib.sha1(f"{role}|{text}".encode("utf-8")).hexdigest()[:16]


def _text_of(content: Any) -> str:
    """Flatten a message's `content` into a single string. Handles both the
    string shortcut and the list-of-blocks form CC uses."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    out: list[str] = []
    for blk in content:
        if not isinstance(blk, dict):
            continue
        t = blk.get("type")
        if t == "text":
            out.append(blk.get("text", ""))
        elif t == "thinking":
            # Exclude thinking blocks from L1 fodder — they're ephemeral.
            continue
        elif t == "tool_use":
            tool_name = blk.get("name", "?")
            tool_input = json.dumps(blk.get("input", {}), ensure_ascii=False)[:400]
            out.append(f"[tool_use:{tool_name} {tool_input}]")
        elif t == "tool_result":
            result = blk.get("content", "")
            if isinstance(result, list):
                result = " ".join(
                    b.get("text", "") for b in result if isinstance(b, dict)
                )
            out.append(f"[tool_result:{str(result)[:400]}]")
    return "\n".join(s for s in out if s)


def parse_messages_body(body: bytes) -> dict | None:
    """Returns None if this isn't a valid /v1/messages request body."""
    try:
        d = json.loads(body)
    except Exception:
        return None
    if "messages" not in d:
        return None
    return d


def extract_session_id(body_json: dict) -> str | None:
    """CC stuffs the session UUID inside the stringified metadata.user_id."""
    meta = body_json.get("metadata") or {}
    uid = meta.get("user_id")
    if not isinstance(uid, str):
        return None
    try:
        inner = json.loads(uid)
        return inner.get("session_id")
    except Exception:
        return None


def messages_to_l1(body_json: dict) -> list[L1Memory]:
    """One L1Memory entry per message. `entities[0]` carries the fingerprint
    so callers can dedupe against what's already been absorbed."""
    out: list[L1Memory] = []
    for msg in body_json.get("messages", []):
        role = msg.get("role", "?")
        text = _text_of(msg.get("content"))
        if not text.strip():
            continue
        fp = _fingerprint(role, text)
        out.append(L1Memory(
            content=f"[{role}] {text[:500]}",
            raw_content=text,
            entities=[fp],
        ))
    return out
