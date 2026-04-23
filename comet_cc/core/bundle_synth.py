"""Bundle synthesis — one extra LLM call per tool-call bundle per compact.

Takes the raw tool-chain produced by extractor._render_tool_bundle and
asks haiku to produce:
  - bundle_summary / bundle_trigger: the whole chain's overall gist
    (the "what did this tool run accomplish" the dialog memory map sees)
  - per_call: one summary+trigger per individual tool_use → tool_result pair

Caller wires the parent + children into the store with `parent_node_id`
+ `links`. Retrieval only shows the parent; drill-down via
`read-node <parent> --links` reveals the per-call children.
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass

from loguru import logger


@dataclass
class BundleChild:
    tool_name: str
    summary: str
    trigger: str


@dataclass
class BundleSynthesis:
    summary: str
    trigger: str
    tags: list[str]
    importance: str  # HIGH/MED/LOW
    children: list[BundleChild]


_PROMPT = (
    "You are summarizing a tool-use chain from an AI coding assistant. "
    "The agent issued N tool calls in sequence; the raw tool_name, "
    "arguments, and output are provided below. Produce:\n\n"
    "1. A BUNDLE summary + trigger describing what the whole chain "
    "accomplished (so the dialog memory layer can represent the tool "
    "activity as a single coherent unit).\n"
    "2. For EACH individual tool call, a one-sentence summary + a "
    "`When I ...` trigger for that call alone.\n\n"
    "Write in the same language as the tool inputs/outputs. Be concrete — "
    "preserve filenames, identifiers, specific values. Skip any call "
    "whose output is clearly noise / boilerplate (e.g., empty stdout).\n\n"
    "--- TOOL CHAIN ---\n{chain}\n--- END ---\n\n"
    "Respond with ONLY valid JSON, no markdown fence:\n"
    "{{\n"
    '  "bundle_summary": "...",\n'
    '  "bundle_trigger": "When I ...",\n'
    '  "tags": ["tag1", "tag2"],\n'
    '  "importance": "HIGH" | "MED" | "LOW",\n'
    '  "calls": [\n'
    '    {{"tool_name": "...", "summary": "...", "trigger": "When I ..."}}\n'
    "  ]\n"
    "}}"
)


def synthesize(chain_text: str, timeout: int = 120) -> BundleSynthesis | None:
    """Run one haiku call; return parsed synthesis or None on failure."""
    if not chain_text.strip():
        return None
    prompt = _PROMPT.format(chain=chain_text[:8000])
    env = {k: v for k, v in os.environ.items()
           if k not in ("ANTHROPIC_BASE_URL", "NODE_EXTRA_CA_CERTS")}
    env["COMET_CC_INTERNAL"] = "1"
    try:
        proc = subprocess.run(
            ["claude", "-p", prompt, "--model", "haiku", "--output-format", "json"],
            capture_output=True, text=True, timeout=timeout, env=env,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        logger.warning(f"bundle synth subprocess failed: {e}")
        return None
    if proc.returncode != 0:
        logger.warning(f"bundle synth rc={proc.returncode}: {proc.stderr[:200]}")
        return None
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    raw = (envelope.get("result") or "").strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        lines = [l for l in lines if not l.startswith("```")]
        raw = "\n".join(lines).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning(f"bundle synth JSON parse failed: {raw[:200]}")
        return None

    imp = (data.get("importance") or "MED").upper()
    if imp not in ("HIGH", "MED", "LOW"):
        imp = "MED"
    children = []
    for c in data.get("calls") or []:
        if not isinstance(c, dict):
            continue
        summary = str(c.get("summary", "")).strip()
        if not summary:
            continue
        children.append(BundleChild(
            tool_name=str(c.get("tool_name", "?")),
            summary=summary,
            trigger=str(c.get("trigger", "")).strip(),
        ))
    return BundleSynthesis(
        summary=str(data.get("bundle_summary", "")).strip(),
        trigger=str(data.get("bundle_trigger", "")).strip(),
        tags=[str(t) for t in (data.get("tags") or [])[:3]],
        importance=imp,
        children=children,
    )
