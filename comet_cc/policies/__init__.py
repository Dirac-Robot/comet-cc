"""Compaction policies — two modalities for the CC plugin.

A CC session is almost always either "user↔assistant conversation" or
"tool activity on files/code", so the plugin collapses to those two.
Policy blocks are intentionally terse: enough to keep the
summary-vs-trigger contract and modality hint, nothing more.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MemoryGenerationPolicy:
    modality: str
    recall_mode: str = "active"
    tag_hints: tuple[str, ...] = ()
    extract_rules: bool = False

    def render_compactor_instructions(self) -> str:
        return _POLICY_BLOCKS.get(self.modality, _POLICY_BLOCKS["dialog"])


DIALOG = MemoryGenerationPolicy(
    modality="dialog",
    extract_rules=True,
)

CODE = MemoryGenerationPolicy(
    modality="code",
    tag_hints=("code",),
)

ALL_POLICIES = {
    "dialog": DIALOG,
    "code": CODE,
}


_POLICY_BLOCKS = {
    "dialog": (
        "This turn is a user↔assistant conversation.\n"
        "- summary: concrete facts, decisions, or preferences from the exchange; "
        "semicolon-separated if multiple topics. Capture explicit user "
        "preferences/corrections verbatim. Convert relative time expressions "
        "to absolute dates using session timestamps.\n"
        "- trigger: when the user would need to reopen this exchange — start "
        "with 'When I...'. Must differ from summary.\n"
        "- recall_mode: active by default; passive for permanent user "
        "instructions; both for critical constraints needing both search and "
        "always-in-context.\n"
        "- session_brief: full rewrite if durable signal emerged (preferences, "
        "active work context, pitfalls). Return empty string to leave prior "
        "brief untouched."
    ),
    "code": (
        "This turn is code or file-system activity (tool bundle touching "
        "source files, configs, or artifacts).\n"
        "- summary: start with language/type and file path. For reads, state "
        "module role + key exports. For edits/writes, state what changed and why.\n"
        "- trigger: 'When I need to inspect or modify <specific thing> in "
        "<file>'. 2-4 anchors, no entity dump.\n"
        "- recall_mode: always 'active'.\n"
        "- topic_tags: must include 'code'.\n"
        "- session_brief: '' (only dialog produces briefs)."
    ),
}


_SESSION_BRIEF_INSTRUCTION = (
    "Rewrite the session brief from scratch — a FULL REWRITE, not an append. "
    "Base it on the prior brief (shown in the 'Previous Session Brief' block "
    "below if present) plus the new signals in this turn. Output the complete "
    "new brief; no 'unchanged' or 'see above' markers.\n\n"
    "Purpose: a short guidance layer that rides in every turn's context, "
    "meant to shape next-turn behavior directly — preferences, "
    "work-in-flight, hints learned from failures or corrections.\n\n"
    "Fixed skeleton; headers in English, body in the USER'S language.\n\n"
    "## Active Work Context\n"
    "  - 2-4 bullets. Current goal, what's in-flight, binding constraints. "
    "Ephemeral only — omit anything stale within a few turns.\n\n"
    "## Hints\n"
    "  - 0-6 bullets. Each bullet combines rule + hint + rationale in one "
    "sentence via a dash clause: 'usually do X — because Y, Z rarely "
    "recovers'. Prefer hedged vocabulary (usually / tends to / rarely) "
    "over binary 'do not'. Sources: user corrections, stable preferences, "
    "approaches that repeatedly failed, patterns the user explicitly "
    "confirmed. One hint per bullet.\n\n"
    "Total length: ≤ 1500 characters. Omit a section with nothing to say. "
    "Return empty string if the session has produced no durable signal yet."
)
