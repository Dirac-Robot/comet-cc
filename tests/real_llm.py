"""Stage 2: real `claude -p` invocation tests.

Verifies that:
  - `claude` CLI is on PATH and --output-format json works
  - haiku sensor returns parseable JSON matching the expected schema
  - sonnet compacter returns parseable JSON matching CompactedResult

Costs a few LLM calls. Skip with `SKIP_LLM=1` env var.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def _header(label: str) -> None:
    print(f"\n=== {label} ===")


def _ok(label: str) -> None:
    print(f"  ✓ {label}")


def _fail(label: str, msg: str) -> None:
    print(f"  ✗ {label}: {msg}")
    sys.exit(1)


def check_claude_cli() -> None:
    _header("0. claude CLI")
    path = shutil.which("claude")
    if not path:
        _fail("claude on PATH", "`claude` not found. Install Claude Code first.")
    _ok(f"claude found at {path}")

    try:
        proc = subprocess.run(
            ["claude", "-p", "reply with the single word OK", "--model", "haiku",
             "--output-format", "json"],
            capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        _fail("claude -p haiku", "timed out after 60s")
    if proc.returncode != 0:
        _fail("claude -p haiku", f"rc={proc.returncode} stderr={proc.stderr[:200]}")
    import json as _json
    try:
        env = _json.loads(proc.stdout)
    except _json.JSONDecodeError:
        _fail("envelope", f"stdout not JSON: {proc.stdout[:200]}")
    if "result" not in env:
        _fail("envelope", f"missing `result` key: {env}")
    _ok(f"envelope.result = {env['result'][:60].strip()!r}")


def test_sensor() -> None:
    _header("1. Sensor (haiku)")
    from comet_cc.core import sensor
    from comet_cc.schemas import L1Memory

    buffer = [
        L1Memory(content="user asked about Python dict comprehension"),
        L1Memory(content="assistant gave a comprehension example with enumerate"),
        L1Memory(content="user confirmed understanding"),
    ]
    load = sensor.assess_load(
        current_input="Now can you explain SQL INNER JOIN vs LEFT JOIN?",
        l1_buffer=buffer,
    )
    print(f"    flow={load.logic_flow} load={load.load_level} "
          f"redundancy={load.redundancy_detected}")
    assert load.logic_flow in ("MAINTAIN", "BROKEN")
    assert 1 <= load.load_level <= 5
    _ok("sensor JSON parses into CognitiveLoad")

    reason = sensor.get_compaction_reason(load, buffer_size=3,
                                          min_l1_buffer=3, load_threshold=4)
    print(f"    compaction_reason = {reason!r}")
    _ok("get_compaction_reason returns valid reason or None")


def test_compacter() -> None:
    _header("2. Compacter (sonnet) — dialog policy")
    from comet_cc.core.compacter import compact
    from comet_cc.policies import DIALOG
    from comet_cc.schemas import L1Memory

    buffer = [
        L1Memory(content="[USER] Can you help me set up a Python logging config?"),
        L1Memory(content="[ASSISTANT] Suggested using loguru with a rotating file sink at ~/.myapp/log. "
                         "Showed a 5-line snippet."),
        L1Memory(content="[USER] Actually I want json-structured logs instead."),
        L1Memory(content="[ASSISTANT] Switched to structlog with a JSONRenderer processor. "
                         "Provided replacement config."),
        L1Memory(content="[USER] Great, that's exactly what I needed. Please never suggest "
                         "plain text logs again."),
    ]
    result = compact(
        l1_buffer=buffer, policy=DIALOG,
        session_id="smoke-s1",
        compaction_reason="topic_shift",
        timeout=240,
    )
    if result is None:
        _fail("compact()", "returned None — LLM call or parse failed")
    node, session_brief = result
    print(f"    node_id = {node.node_id}")
    print(f"    summary = {node.summary}")
    print(f"    trigger = {node.trigger}")
    print(f"    recall_mode = {node.recall_mode}   importance = {node.importance}")
    print(f"    topic_tags = {node.topic_tags}")
    print(f"    session_brief (len={len(session_brief)}):")
    for line in session_brief.splitlines()[:12]:
        print(f"      {line}")
    assert node.summary, "summary empty"
    assert node.trigger and node.trigger != node.summary, \
        "trigger must differ from summary"
    _ok("compacter produced structured node with distinct summary/trigger")
    assert node.recall_mode in ("active", "passive", "both")
    assert node.importance in ("HIGH", "MED", "LOW")
    _ok("recall_mode + importance within allowed set")
    if session_brief.strip():
        _ok(f"session brief emitted ({len(session_brief)} chars)")
    else:
        print("    ~ session brief empty (DIALOG policy — LLM chose no durable signal yet)")


def test_compacter_code() -> None:
    _header("3. Compacter — code policy")
    from comet_cc.core.compacter import compact
    from comet_cc.policies import CODE
    from comet_cc.schemas import L1Memory

    buffer = [
        L1Memory(content="[TOOL_BUNDLE]\n"
                         "  - Read(backend/auth.py) → [OK] 142 lines, "
                         "exports authenticate(), verify_token(), refresh_session()\n"
                         "  - Edit(backend/auth.py) → [OK] added RBAC check in "
                         "authenticate() to enforce role_required parameter"),
    ]
    result = compact(
        l1_buffer=buffer, policy=CODE,
        session_id="smoke-s1", compaction_reason="high_load",
        timeout=240,
    )
    if result is None:
        _fail("compact()", "returned None for code")
    node, brief = result
    print(f"    summary = {node.summary}")
    print(f"    trigger = {node.trigger}")
    print(f"    tags    = {node.topic_tags}")
    assert brief == "" or not brief.strip(), \
        "code policy must not emit a session brief"
    _ok("code policy emits empty session_brief")
    assert node.recall_mode == "active"
    _ok("code → recall_mode=active")
    assert "code" in [t.lower() for t in node.topic_tags], \
        f"code policy must include 'code' tag, got {node.topic_tags}"
    _ok("code tag present")


def main() -> int:
    if os.environ.get("SKIP_LLM"):
        print("SKIP_LLM set — exiting.")
        return 0
    check_claude_cli()
    test_sensor()
    test_compacter()
    test_compacter_code()
    print("\nStage-2 real LLM tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
