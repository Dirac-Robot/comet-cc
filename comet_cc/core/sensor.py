"""Sensor — cheap per-turn gate via `claude -p --model haiku`.

Emits CognitiveLoad signals; get_compaction_reason() maps them to a reason
string (topic_shift / high_load / buffer_overflow) or None.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from loguru import logger

from comet_cc.schemas import CognitiveLoad, L1Memory

_TEMPLATE_PATH = Path(__file__).parent.parent / "templates" / "cognitive_load.txt"
_TEMPLATE = _TEMPLATE_PATH.read_text(encoding="utf-8")

_JSON_INSTRUCTION = (
    "\n\n## Output Format (STRICT)\n"
    "Respond with ONLY valid JSON, no markdown fence, no prose:\n"
    '{"logic_flow": "MAINTAIN"|"BROKEN", '
    '"load_level": 1-5, '
    '"redundancy_detected": true|false}'
)


def assess_load(
    current_input: str,
    l1_buffer: list[L1Memory],
    session_summaries: list[str] | None = None,
    timeout: int = 30,
) -> CognitiveLoad:
    """Assess the current turn against the running buffer. Returns signals
    used to decide whether to compact now."""
    l1_summaries = (
        "\n".join(f"- {mem.content}" for mem in l1_buffer[-5:])
        if l1_buffer else "(No previous context)"
    )
    session_summaries_text = (
        "\n".join(f"- {s}" for s in session_summaries)
        if session_summaries else "(No session memory yet)"
    )

    prompt = _TEMPLATE.format(
        l1_summaries=l1_summaries,
        current_input=current_input,
        session_summaries=session_summaries_text,
    ) + _JSON_INSTRUCTION

    raw = _invoke_claude(prompt, model="haiku", timeout=timeout)
    if raw is None:
        return CognitiveLoad()  # fail-safe MAINTAIN/1/False

    data = _parse_json(raw)
    if not data:
        return CognitiveLoad()

    return CognitiveLoad(
        logic_flow=data.get("logic_flow", "MAINTAIN"),
        load_level=int(data.get("load_level", 1)),
        redundancy_detected=bool(data.get("redundancy_detected", False)),
    )


def get_compaction_reason(
    load: CognitiveLoad,
    buffer_size: int,
    max_l1_buffer: int = 20,
    min_l1_buffer: int = 3,
    load_threshold: int = 4,
) -> str | None:
    """Semantic gate — topic shift / high load / buffer overflow."""
    if buffer_size < min_l1_buffer:
        return None
    if load.logic_flow == "BROKEN":
        return "topic_shift"
    if load.load_level >= load_threshold:
        return "high_load"
    if buffer_size >= max_l1_buffer:
        return "buffer_overflow"
    return None


def _invoke_claude(prompt: str, model: str, timeout: int) -> str | None:
    import os as _os
    # Strip proxy env — child `claude -p` must hit real Anthropic directly,
    # not our own proxy (would infinite-loop on sensor's own traffic).
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
    """Strip optional markdown fence, parse JSON. None on failure."""
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
        logger.warning(f"sensor JSON parse failed: {s[:200]}")
        return None
