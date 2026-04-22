"""CC transcript jsonl parser — turn-level bundling.

Groups raw Claude Code transcript entries into logical buffer units:
  - USER_TEXT: user's actual message (not a tool_result)
  - TOOL_BUNDLE: consecutive assistant.tool_use + user.tool_result pairs
                 until the assistant breaks with a text block
  - ASSISTANT_TEXT: final assistant reply closing a turn
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from comet_cc.schemas import L1Memory

NodeKind = Literal["user_text", "tool_bundle", "assistant_text"]


@dataclass
class LogicalNode:
    kind: NodeKind
    content: str
    raw_content: str
    entry_uuids: list[str]
    has_tools: bool = False
    tool_names: list[str] = None

    def to_l1(self) -> L1Memory:
        return L1Memory(
            content=self.content,
            raw_content=self.raw_content,
        )


def parse_transcript(path: str | Path) -> list[LogicalNode]:
    """Read CC transcript jsonl → ordered list of logical nodes."""
    p = Path(path)
    if not p.exists():
        return []

    entries = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    nodes: list[LogicalNode] = []
    i = 0
    while i < len(entries):
        e = entries[i]
        etype = e.get("type")

        if etype == "user" and _is_pure_user_text(e):
            nodes.append(_user_node(e))
            i += 1
            continue

        if etype == "assistant" and _has_tool_use(e):
            bundle_entries = []
            while i < len(entries):
                cur = entries[i]
                ctype = cur.get("type")
                if ctype == "assistant" and _has_tool_use(cur):
                    bundle_entries.append(cur)
                    i += 1
                    continue
                if ctype == "user" and _has_tool_result(cur):
                    bundle_entries.append(cur)
                    i += 1
                    continue
                break
            nodes.append(_bundle_node(bundle_entries))
            continue

        if etype == "assistant":
            nodes.append(_assistant_text_node(e))
            i += 1
            continue

        i += 1

    return nodes


def _blocks(entry: dict) -> list[dict]:
    msg = entry.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return content
    return []


def _is_pure_user_text(entry: dict) -> bool:
    blocks = _blocks(entry)
    if not blocks:
        return False
    return not any(b.get("type") == "tool_result" for b in blocks)


def _has_tool_use(entry: dict) -> bool:
    return any(b.get("type") == "tool_use" for b in _blocks(entry))


def _has_tool_result(entry: dict) -> bool:
    return any(b.get("type") == "tool_result" for b in _blocks(entry))


def _text_from_blocks(blocks: list[dict]) -> str:
    parts = []
    for b in blocks:
        if b.get("type") == "text":
            t = b.get("text", "")
            if t:
                parts.append(t)
    return "\n".join(parts)


def _user_node(entry: dict) -> LogicalNode:
    blocks = _blocks(entry)
    text = _text_from_blocks(blocks)
    return LogicalNode(
        kind="user_text",
        content=f"[USER] {text[:400]}",
        raw_content=text,
        entry_uuids=[entry.get("uuid", "")],
    )


def _assistant_text_node(entry: dict) -> LogicalNode:
    blocks = _blocks(entry)
    text = _text_from_blocks(blocks)
    return LogicalNode(
        kind="assistant_text",
        content=f"[ASSISTANT] {text[:400]}",
        raw_content=text,
        entry_uuids=[entry.get("uuid", "")],
    )


def _bundle_node(entries: list[dict]) -> LogicalNode:
    """Render a tool bundle: list of (tool_name, input preview → output preview)."""
    tool_names: list[str] = []
    lines: list[str] = []
    raw_parts: list[str] = []
    pending_calls: dict[str, tuple[str, str]] = {}  # tool_use_id → (name, input_preview)

    for e in entries:
        for b in _blocks(e):
            btype = b.get("type")
            if btype == "tool_use":
                tid = b.get("id", "")
                name = b.get("name", "?")
                tool_names.append(name)
                inp = json.dumps(b.get("input", {}), ensure_ascii=False)[:300]
                pending_calls[tid] = (name, inp)
            elif btype == "tool_result":
                tid = b.get("tool_use_id", "")
                name, inp = pending_calls.pop(tid, ("?", ""))
                content = b.get("content", "")
                if isinstance(content, list):
                    out = "\n".join(
                        c.get("text", "") if isinstance(c, dict) else str(c)
                        for c in content
                    )
                else:
                    out = str(content)
                is_err = bool(b.get("is_error"))
                status = "ERR" if is_err else "OK"
                line = f"  - {name}({inp}) → [{status}] {out[:200]}"
                lines.append(line)
                raw_parts.append(f"{name}({inp}) → {out}")

    for tid, (name, inp) in pending_calls.items():
        lines.append(f"  - {name}({inp}) → (no result yet)")
        raw_parts.append(f"{name}({inp}) → pending")

    content = "[TOOL_BUNDLE]\n" + "\n".join(lines) if lines else "[TOOL_BUNDLE] (empty)"
    return LogicalNode(
        kind="tool_bundle",
        content=content[:1500],
        raw_content="\n\n".join(raw_parts),
        entry_uuids=[e.get("uuid", "") for e in entries],
        has_tools=True,
        tool_names=tool_names,
    )


def choose_policy_for_bundle(node: LogicalNode) -> str:
    """Pick compaction policy. Tool bundles → code; everything else → dialog."""
    if node.kind == "tool_bundle":
        return "code"
    return "dialog"
