"""Compacter — structured node synthesis via `claude -p --model sonnet`.

Renders a policy-specific prompt, calls the model, parses the JSON result
into a MemoryNode + optional session brief. Side-effect-free — the caller
handles store persistence so retries are trivial.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from loguru import logger

from comet_cc.policies import _SESSION_BRIEF_INSTRUCTION, MemoryGenerationPolicy
from comet_cc.schemas import CompactedResult, L1Memory, MemoryNode

_TEMPLATE_PATH = Path(__file__).parent.parent / "templates" / "compacting_base.txt"
_TEMPLATE = _TEMPLATE_PATH.read_text(encoding="utf-8")

_META_PREFIXES = ("ORIGIN:", "FLAG:", "SESSION:", "IMPORTANCE:")

# Recognized turn-role prefixes (extractor.py emits "[user] ..." / "[assistant]
# ..." per Anthropic API messages). Anything else (e.g. "[tool_bundle]") stays
# inline as a free-form bracket tag — only matches in this set are promoted to
# a role label so the prompt can show "USER vs ASSISTANT" cleanly.
_ROLE_ALIASES = {
    "user": "USER",
    "human": "USER",
    "assistant": "ASSISTANT",
    "ai": "ASSISTANT",
    "system": "SYSTEM",
    "session": "SYSTEM",
    "tool": "TOOL",
}
_ROLE_PREFIX_RE = re.compile(r"^\[([a-zA-Z][\w-]*)\]\s*", re.DOTALL)


def _split_role(content: str) -> tuple[str | None, str]:
    m = _ROLE_PREFIX_RE.match(content)
    if not m:
        return None, content
    role = _ROLE_ALIASES.get(m.group(1).lower())
    if role is None:
        return None, content
    return role, content[m.end():]


def _format_turns_for_prompt(l1_buffer: list[L1Memory]) -> str:
    """Render L1 buffer as role-labeled chat blocks for the compacter prompt.

    Turns whose content starts with a recognized [role] prefix become
    ``ROLE:\\n<body>`` blocks; other entries (tool bundles, external
    content) fall through as bare bullet lines.
    """
    blocks: list[str] = []
    for mem in l1_buffer:
        role, body = _split_role(mem.content)
        if role:
            blocks.append(f"{role}:\n{body}")
        else:
            blocks.append(f"- {mem.content}")
    return "\n\n".join(blocks)

_JSON_INSTRUCTION = (
    "\n\n## Output Format (STRICT)\n"
    "Respond with ONLY valid JSON, no markdown fence, no prose. Schema:\n"
    "{\n"
    '  "summary": "...",\n'
    '  "trigger": "...",\n'
    '  "recall_mode": "passive"|"active"|"both",\n'
    '  "topic_tags": ["tag1", "tag2"],\n'
    '  "importance": "HIGH"|"MED"|"LOW",\n'
    '  "session_brief": "..."\n'
    "}"
)


def compact(
    l1_buffer: list[L1Memory],
    policy: MemoryGenerationPolicy,
    *,
    session_id: str | None = None,
    compaction_reason: str | None = None,
    existing_tags: set[str] | None = None,
    existing_brief: str = "",
    preceding_summaries: list[str] | None = None,
    language: str = "the same language as the user",
    timeout: int = 180,
) -> MemoryNode | None:
    """Compact L1 buffer → MemoryNode. Returns None on LLM failure.

    Caller is responsible for store.save_node() and vector_index upsert —
    compacter stays side-effect-free so retries are trivial.
    """
    # Render turns as role-labeled blocks so summary can preserve
    # who-said-what (user request vs assistant action) instead of
    # flattening everything into one assistant-side narrative.
    turns_text = _format_turns_for_prompt(l1_buffer)

    tag_pool = existing_tags or set()
    tag_pool = {t for t in tag_pool if not any(t.startswith(p) for p in _META_PREFIXES)}
    tags_text = ", ".join(sorted(tag_pool)) if tag_pool else "(none)"

    preceding_context = ""
    if preceding_summaries:
        lines = "\n".join(f"  - {s}" for s in preceding_summaries)
        preceding_context = (
            "### Preceding User Summaries (already indexed — avoid repeating)\n"
            f"{lines}\n\n"
        )
    if existing_brief.strip():
        preceding_context += (
            "### Previous Session Brief (for reference when rewriting)\n"
            f"{existing_brief.strip()}\n\n"
        )

    summary_instr, trigger_instr, recall_instr = _modality_instructions(policy.modality)

    brief_instr = (
        _SESSION_BRIEF_INSTRUCTION
        if policy.extract_rules
        else 'Return empty string "". Session briefs are only produced for dialog-modality nodes.'
    )

    extra_tag = (
        f'- MUST include: {", ".join(policy.tag_hints)}'
        if policy.tag_hints else ""
    )

    prompt = _TEMPLATE.format(
        turns=turns_text,
        policy_block=policy.render_compactor_instructions(),
        summary_instruction=summary_instr,
        trigger_instruction=trigger_instr,
        recall_instruction=recall_instr,
        existing_tags=tags_text,
        extra_tag_instruction=extra_tag,
        brief_instruction=brief_instr,
        preceding_context=preceding_context,
        language=language,
    ) + _JSON_INSTRUCTION

    raw = _invoke_claude(prompt, model="sonnet", timeout=timeout)
    if raw is None:
        return None
    data = _parse_json(raw)
    if not data:
        return None

    result = CompactedResult(
        summary=str(data.get("summary", "")).strip(),
        trigger=str(data.get("trigger", "")).strip(),
        recall_mode=data.get("recall_mode", "active")
            if data.get("recall_mode") in ("passive", "active", "both") else "active",
        topic_tags=list(data.get("topic_tags", []))[:3],
        importance=(data.get("importance") or "MED").upper()
            if (data.get("importance") or "MED").upper() in ("HIGH", "MED", "LOW") else "MED",
        session_brief=str(data.get("session_brief", "") or "").strip(),
    )
    if not result.summary:
        logger.warning("compacter produced empty summary — skipping node creation")
        return None

    tags = [t for t in result.topic_tags
            if not any(t.startswith(p) for p in _META_PREFIXES)]
    for hint in policy.tag_hints:
        if hint not in tags:
            tags.append(hint)
    tags.append(f"IMPORTANCE:{result.importance}")

    return MemoryNode(
        node_id=MemoryNode.new_id(),
        session_id=session_id,
        depth_level=1,
        recall_mode=result.recall_mode,
        topic_tags=tags,
        summary=result.summary,
        trigger=result.trigger,
        importance=result.importance,
        compaction_reason=compaction_reason,
    ), result.session_brief


def _modality_instructions(modality: str) -> tuple[str, str, str]:
    """Per-field instructions injected into compacting_base template slots.
    Only two modalities — the policy block (`## Policy`) carries the rest."""
    if modality == "code":
        return (
            "Start with language/type + file path. State module role / key "
            "exports on reads, or what changed + why on edits/writes.",
            '"When I need to inspect or modify <specific thing> in <file>". '
            "Max 2-4 anchors.",
            'Always "active" for code.',
        )
    return (
        "Concrete facts, decisions, or preferences; semicolon-separated if "
        "multiple topics. Capture explicit user preferences/corrections verbatim. "
        "Convert relative time expressions to absolute dates using session "
        "timestamps.",
        "Start with 'When I...'. Describes when the user would need to reopen "
        "this exchange. Must differ from summary. 2-4 anchors.",
        "active (default), passive (permanent instructions), both (critical "
        "constraints).",
    )


def _invoke_claude(prompt: str, model: str, timeout: int) -> str | None:
    import os as _os
    # Strip proxy env — child `claude -p` must hit real Anthropic directly,
    # not our own proxy (would infinite-loop on compacter's own traffic).
    env = {k: v for k, v in _os.environ.items()
           if k not in ("ANTHROPIC_BASE_URL", "NODE_EXTRA_CA_CERTS")}
    env["COMET_CC_INTERNAL"] = "1"
    try:
        proc = subprocess.run(
            ["claude", "-p", prompt, "--model", model, "--output-format", "json"],
            capture_output=True, text=True, timeout=timeout, env=env,
        )
    except subprocess.TimeoutExpired:
        logger.warning(f"claude -p {model} timed out")
        return None
    except FileNotFoundError:
        logger.error("`claude` CLI not found on PATH")
        return None
    if proc.returncode != 0:
        logger.warning(f"claude -p returned {proc.returncode}: {proc.stderr[:200]}")
        return None
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError:
        logger.warning(f"claude envelope not JSON: {proc.stdout[:200]}")
        return None
    return envelope.get("result")


def _parse_json(raw: str) -> dict | None:
    s = raw.strip()
    if s.startswith("```"):
        lines = s.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        s = "\n".join(lines).strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        logger.warning(f"compacter JSON parse failed: {s[:200]}")
        return None
