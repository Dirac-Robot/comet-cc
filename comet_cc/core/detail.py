"""Tier-2 detailed summary generator — lazy expansion from tier-3 raw turns.

Called on `read-node --depth 1` when a node has no cached detailed_summary.
Uses haiku (cheap) since the prompt is narrow and the raw is bounded to ~6KB.
"""
from __future__ import annotations

import json
import os
import subprocess

from loguru import logger


_PROMPT = (
    "Given the following raw turns from a memory node, write a detailed "
    "summary (3-8 sentences) that captures the KEY facts, entities, "
    "relationships, and conclusions. Preserve specific numbers, names, "
    "and technical details. Do NOT include filler phrases like 'This "
    "document discusses...'. Respond in the same language as the input.\n\n"
    "--- RAW TURNS ---\n{raw}\n--- END ---\n\n"
    "Detailed Summary:"
)


def generate_detailed_summary(raw_text: str, timeout: int = 60) -> str | None:
    if not raw_text.strip():
        return None
    prompt = _PROMPT.format(raw=raw_text[:6000])
    env = {k: v for k, v in os.environ.items()
           if k not in ("ANTHROPIC_BASE_URL", "NODE_EXTRA_CA_CERTS")}
    env["COMET_CC_INTERNAL"] = "1"
    try:
        proc = subprocess.run(
            ["claude", "-p", prompt, "--model", "haiku", "--output-format", "json"],
            capture_output=True, text=True, timeout=timeout, env=env,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        logger.warning(f"detail gen subprocess failed: {e}")
        return None
    if proc.returncode != 0:
        logger.warning(f"detail gen rc={proc.returncode}: {proc.stderr[:200]}")
        return None
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    return (envelope.get("result") or "").strip() or None
