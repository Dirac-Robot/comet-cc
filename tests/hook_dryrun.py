"""Stage 3: hook dry-run — pipe synthetic CC payloads into each hook module.

Exercises the full E2E flow without touching ~/.claude/settings.json:
  1. SessionStart — ensure store init idempotent
  2. Stop      — synthetic transcript → sensor → compacter → node saved
  3. UserPromptSubmit — retrieve injected node as additionalContext
  4. PreCompact — stored summary surfaces as custom context

Uses a throwaway COMET_CC_HOME so the user's real store is untouched.
Costs a few LLM calls (sensor + compacter).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import sqlite3
import tempfile
import time
from pathlib import Path


_ROOT = Path(__file__).parent.parent
_PY = sys.executable


def _run_hook(module: str, payload: dict, env: dict) -> tuple[int, str, str]:
    proc = subprocess.run(
        [_PY, "-m", module],
        input=json.dumps(payload),
        capture_output=True, text=True,
        env={**os.environ, **env},
        cwd=_ROOT,
        timeout=300,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _tail_log(home: Path, n: int = 30) -> None:
    log = home / "logs" / "hook.log"
    if not log.exists():
        print("    (no log file)")
        return
    lines = log.read_text(encoding="utf-8").splitlines()
    print(f"    --- last {min(n, len(lines))} log lines ---")
    for line in lines[-n:]:
        print(f"    | {line}")


def _header(label: str) -> None:
    print(f"\n=== {label} ===")


def _synth_transcript(path: Path) -> None:
    """Write a CC-style jsonl where the last turn clearly pivots topics —
    should trip the sensor with logic_flow=BROKEN."""
    entries = [
        {
            "type": "user", "uuid": "u1",
            "message": {"role": "user", "content": [
                {"type": "text", "text":
                 "I want to refactor our payment webhook handler to use async SQLAlchemy. "
                 "Currently it uses sync sessions inside a FastAPI endpoint."},
            ]},
        },
        {
            "type": "assistant", "uuid": "a1", "parentUuid": "u1",
            "message": {"role": "assistant", "content": [
                {"type": "text", "text":
                 "Switched webhook to AsyncSession with asyncpg. Replaced all Session.query "
                 "calls with select() + execute. Added async context manager around commits."},
            ]},
        },
        {
            "type": "user", "uuid": "u2", "parentUuid": "a1",
            "message": {"role": "user", "content": [
                {"type": "text", "text":
                 "Great. The integration tests for it now pass. Let's commit this."},
            ]},
        },
        {
            "type": "assistant", "uuid": "a2", "parentUuid": "u2",
            "message": {"role": "assistant", "content": [
                {"type": "text", "text":
                 "Committed as `refactor(webhook): migrate to async SQLAlchemy` on feature/async-webhook."},
            ]},
        },
        {
            "type": "user", "uuid": "u3", "parentUuid": "a2",
            "message": {"role": "user", "content": [
                {"type": "text", "text":
                 "Now switching topics entirely. Can you help me plan a marketing "
                 "email campaign for a product launch?"},
            ]},
        },
    ]
    path.write_text("\n".join(json.dumps(e) for e in entries), encoding="utf-8")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="comet-cc-e2e-") as td:
        home = Path(td)
        env = {"COMET_CC_HOME": str(home), "COMET_CC_MIN_L1": "2"}
        # Propagate to this test process too so daemon_mgmt.stop() targets the
        # daemon that SessionStart auto-spawned inside `home`.
        os.environ["COMET_CC_HOME"] = str(home)
        os.environ["COMET_CC_MIN_L1"] = "2"
        print(f"COMET_CC_HOME = {home}")

        session_id = "e2e-session-42"
        transcript_path = home / "transcript.jsonl"
        _synth_transcript(transcript_path)

        # --- 1. SessionStart ---
        _header("1. SessionStart")
        rc, out, err = _run_hook(
            "comet_cc.hooks.session_start",
            {"session_id": session_id, "transcript_path": str(transcript_path),
             "hook_event_name": "SessionStart"},
            env,
        )
        print(f"    rc={rc} stdout={out[:80]!r}")
        if err.strip():
            print(f"    stderr: {err.strip()[:200]}")
        assert rc == 0
        store_path = home / "store.sqlite"
        assert store_path.exists(), "store.sqlite not created"
        print(f"    ✓ store created at {store_path}")

        # --- 2. Stop (will invoke sensor + compacter on LLM) ---
        _header("2. Stop — sensor + compacter")
        rc, out, err = _run_hook(
            "comet_cc.hooks.stop",
            {"session_id": session_id, "transcript_path": str(transcript_path),
             "hook_event_name": "Stop"},
            env,
        )
        print(f"    rc={rc}")
        if err.strip():
            # loguru writes to stderr in some configs, filter noise
            for line in err.splitlines()[-20:]:
                print(f"    stderr: {line}")
        assert rc == 0

        # Stop is fire-and-forget when daemon is running — wait for async compact.
        print("    waiting up to 60s for daemon worker to finish compact...")
        deadline = time.monotonic() + 60
        rows: list = []
        while time.monotonic() < deadline:
            conn = sqlite3.connect(str(store_path))
            rows = conn.execute(
                "SELECT node_id, summary, trigger, recall_mode, importance, "
                "compaction_reason, topic_tags FROM nodes"
            ).fetchall()
            conn.close()
            if rows:
                break
            time.sleep(1.0)

        _tail_log(home, n=40)

        print(f"    nodes in store: {len(rows)}")
        for r in rows:
            print(f"      [{r[0]}] imp={r[4]} recall={r[3]} reason={r[5]}")
            print(f"          summary: {r[1][:120]}")
            print(f"          trigger: {r[2][:120]}")
            print(f"          tags   : {r[6]}")
        conn2 = sqlite3.connect(str(store_path))
        brief_rows = conn2.execute(
            "SELECT session_id, LENGTH(brief), SUBSTR(brief, 1, 200) "
            "FROM session_briefs"
        ).fetchall()
        conn2.close()
        for r in brief_rows:
            print(f"    session_brief[{r[0]}] len={r[1]} preview={r[2]!r}")
        assert rows, "expected at least one node after Stop"

        # --- 3. UserPromptSubmit (should surface stored node) ---
        _header("3. UserPromptSubmit — retrieve injection")
        rc, out, err = _run_hook(
            "comet_cc.hooks.user_prompt",
            {"session_id": session_id, "transcript_path": str(transcript_path),
             "hook_event_name": "UserPromptSubmit",
             "prompt": "Can we undo the async SQLAlchemy migration if we hit issues?"},
            env,
        )
        print(f"    rc={rc}")
        if err.strip():
            for line in err.splitlines()[-5:]:
                print(f"    stderr: {line}")
        assert rc == 0
        if out.strip():
            payload_out = json.loads(out)
            ctx = payload_out.get("hookSpecificOutput", {}).get("additionalContext", "")
            print(f"    additionalContext ({len(ctx)} chars):")
            for line in ctx.splitlines()[:24]:
                print(f"      {line}")
            assert "Session Brief" in ctx or "Retrieved Memory" in ctx
        else:
            print("    (empty output — store retrieval returned nothing)")

        # --- 4. PreCompact ---
        _header("4. PreCompact — surface stored summaries")
        rc, out, err = _run_hook(
            "comet_cc.hooks.pre_compact",
            {"session_id": session_id, "transcript_path": str(transcript_path),
             "hook_event_name": "PreCompact", "trigger": "auto"},
            env,
        )
        print(f"    rc={rc}")
        if err.strip():
            for line in err.splitlines()[-5:]:
                print(f"    stderr: {line}")
        assert rc == 0
        if out.strip():
            payload_out = json.loads(out)
            ctx = payload_out.get("hookSpecificOutput", {}).get("additionalContext", "")
            print(f"    custom_instructions ({len(ctx)} chars):")
            for line in ctx.splitlines()[:16]:
                print(f"      {line}")
            assert "pre-digested" in ctx or "Retrieved Memory" in ctx

        # Stop any daemon that SessionStart may have spawned.
        from comet_cc import daemon_mgmt
        daemon_mgmt.stop()

    print("\nStage-3 hook dry-run passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
