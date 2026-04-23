"""Parse an outgoing /v1/messages request body into something the sensor
can reason about: session id + a list of L1Memory entries (one per message).

CC resends its full local transcript on every turn, so fingerprinting by
content hash is enough to tell "what's new since last request".
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from comet_cc.schemas import L1Memory


# CC wraps environment preamble (available skills, workspace notices, etc.)
# in <system-reminder>...</system-reminder> blocks INSIDE the user message's
# text. The same preamble repeats on every turn, so if we leave it in the
# sensor's view every turn looks identical -> sensor never detects topic
# shifts. Strip these blocks when extracting text.
_SYSTEM_REMINDER_RE = re.compile(
    r"<system-reminder>.*?</system-reminder>", re.DOTALL,
)


def _strip_boilerplate(text: str) -> str:
    cleaned = _SYSTEM_REMINDER_RE.sub("", text)
    return cleaned.strip()


def _fingerprint(role: str, text: str) -> str:
    return hashlib.sha1(f"{role}|{text}".encode("utf-8")).hexdigest()[:16]


def _text_of(content: Any) -> str:
    """Flatten a message's `content` into a single string. Handles both the
    string shortcut and the list-of-blocks form CC uses. Strips
    <system-reminder> preamble — it's identical across turns and drowns out
    the actual user intent when the sensor compares buffer entries."""
    if isinstance(content, str):
        return _strip_boilerplate(content)
    if not isinstance(content, list):
        return ""
    out: list[str] = []
    for blk in content:
        if not isinstance(blk, dict):
            continue
        t = blk.get("type")
        if t == "text":
            cleaned = _strip_boilerplate(blk.get("text", ""))
            if cleaned:
                out.append(cleaned)
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


def _has_block(msg: dict, block_type: str) -> bool:
    c = msg.get("content")
    if not isinstance(c, list):
        return False
    return any(isinstance(b, dict) and b.get("type") == block_type for b in c)


def _render_tool_bundle(bundle_msgs: list[dict]) -> tuple[str, str, list[str]]:
    """Returns (content_preview, raw_content, fingerprints_per_msg).
    Fingerprints align 1:1 with `bundle_msgs` so rewrite absorption can
    still mask individual messages."""
    lines: list[str] = []
    raw_parts: list[str] = []
    fps: list[str] = []
    pending: dict[str, tuple[str, str]] = {}  # tool_use_id → (name, input)
    tool_names: list[str] = []

    for msg in bundle_msgs:
        role = msg.get("role", "?")
        text = _text_of(msg.get("content"))
        fps.append(_fingerprint(role, text))
        for blk in msg.get("content") or []:
            if not isinstance(blk, dict):
                continue
            t = blk.get("type")
            if t == "tool_use":
                tid = blk.get("id", "")
                name = blk.get("name", "?")
                tool_names.append(name)
                inp = json.dumps(blk.get("input", {}), ensure_ascii=False)[:300]
                pending[tid] = (name, inp)
            elif t == "tool_result":
                tid = blk.get("tool_use_id", "")
                name, inp = pending.pop(tid, ("?", ""))
                result = blk.get("content", "")
                if isinstance(result, list):
                    out = "\n".join(
                        b.get("text", "") if isinstance(b, dict) else str(b)
                        for b in result
                    )
                else:
                    out = str(result)
                status = "ERR" if blk.get("is_error") else "OK"
                lines.append(f"  - {name}({inp}) → [{status}] {out[:200]}")
                raw_parts.append(f"{name}({inp}) → {out}")
    for tid, (name, inp) in pending.items():
        lines.append(f"  - {name}({inp}) → (no result)")
        raw_parts.append(f"{name}({inp}) → pending")

    preview = "[tool_bundle]\n" + "\n".join(lines) if lines else "[tool_bundle] (empty)"
    raw = "\n\n".join(raw_parts)
    return preview[:1500], raw, fps


def bundle_l1(body_json: dict) -> list[L1Memory]:
    """Bundled view for sensor/compacter.

    Groups runs of (assistant tool_use) + (user tool_result) into a single
    L1Memory of kind [tool_bundle]. `entities` holds the per-message
    fingerprints so the orchestrator can still mark absorption one message
    at a time when trimming the outgoing request."""
    msgs = body_json.get("messages", []) or []
    out: list[L1Memory] = []
    i = 0
    while i < len(msgs):
        msg = msgs[i]
        role = msg.get("role", "?")

        # Run of tool_use (assistant) / tool_result (user) — bundle together.
        if role == "assistant" and _has_block(msg, "tool_use"):
            bundle = []
            while i < len(msgs):
                cur = msgs[i]
                crole = cur.get("role")
                if crole == "assistant" and _has_block(cur, "tool_use"):
                    bundle.append(cur); i += 1; continue
                if crole == "user" and _has_block(cur, "tool_result"):
                    bundle.append(cur); i += 1; continue
                break
            preview, raw, fps = _render_tool_bundle(bundle)
            out.append(L1Memory(content=preview, raw_content=raw, entities=fps))
            continue

        # Plain user / assistant text.
        text = _text_of(msg.get("content"))
        if text.strip():
            fp = _fingerprint(role, text)
            out.append(L1Memory(
                content=f"[{role}] {text[:500]}",
                raw_content=text,
                entities=[fp],
            ))
        i += 1
    return out
