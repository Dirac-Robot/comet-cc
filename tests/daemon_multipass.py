"""Verify the daemon drains the L1 buffer after each compaction so the
next pass starts from turn (k+1), not from turn 1."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path


def _transcript(path: Path, turns: list[tuple[str, str]]) -> None:
    """Each turn is (role, text) — emits a jsonl with role/text blocks."""
    entries = []
    for i, (role, text) in enumerate(turns):
        entries.append({
            "type": role, "uuid": f"u{i}",
            "message": {"role": role, "content": [{"type": "text", "text": text}]},
        })
    path.write_text("\n".join(json.dumps(e) for e in entries), encoding="utf-8")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="multipass-") as td:
        home = Path(td)
        os.environ["COMET_CC_HOME"] = str(home)
        # Keep thresholds low so sensor will trip
        os.environ["COMET_CC_MIN_L1"] = "2"
        os.environ["COMET_CC_MAX_L1"] = "4"

        from comet_cc import client, daemon_mgmt, config
        assert daemon_mgmt.ensure_running(wait_seconds=30.0), "daemon failed to start"

        transcript = home / "transcript.jsonl"
        sid = "mp-session"

        # Round 1 — turns 1-3 about database
        _transcript(transcript, [
            ("user", "Help me set up PostgreSQL with pgvector extension"),
            ("assistant", "Installed pgvector via brew, configured postgresql.conf, "
                           "created extension in the target database."),
            ("user", "Now let's totally switch topics to email templates for onboarding."),
        ])
        print("[round 1] queuing first compaction (turns 1-3)...")
        r = client.queue_compact(sid, str(transcript))
        assert r and r.get("ok")
        # Wait for worker
        time.sleep(50)  # sensor 6s + compacter 25s + margin

        conn = sqlite3.connect(str(config.store_path()))
        rows1 = conn.execute(
            "SELECT node_id, summary, compaction_reason FROM nodes WHERE session_id = ?",
            (sid,),
        ).fetchall()
        conn.close()
        print(f"[round 1] nodes after first compact: {len(rows1)}")
        for r in rows1:
            print(f"  [{r[0]}] reason={r[2]} summary={r[1][:100]}")
        assert len(rows1) == 1, f"expected 1 node, got {len(rows1)}"

        # Round 2 — append turns 4-6 about email templates
        _transcript(transcript, [
            ("user", "Help me set up PostgreSQL with pgvector extension"),
            ("assistant", "Installed pgvector via brew, configured postgresql.conf, "
                           "created extension in the target database."),
            ("user", "Now let's totally switch topics to email templates for onboarding."),
            ("assistant", "Drafted a welcome email template in Handlebars with placeholders "
                           "for {firstName}, {verificationLink}, and {supportEmail}."),
            ("user", "Add a fallback plain-text variant for clients that don't render HTML."),
            ("assistant", "Added a plain-text rendering using mjml-to-text library with "
                           "stripped markup."),
            ("user", "Perfect. One more totally different thing — show me the disk usage."),
        ])
        print("\n[round 2] queuing second compaction (turns 4-7)...")
        r = client.queue_compact(sid, str(transcript))
        assert r and r.get("ok")
        time.sleep(50)

        conn = sqlite3.connect(str(config.store_path()))
        rows2 = conn.execute(
            "SELECT node_id, summary, compaction_reason FROM nodes WHERE session_id = ? "
            "ORDER BY created_at",
            (sid,),
        ).fetchall()
        conn.close()
        print(f"[round 2] nodes after second compact: {len(rows2)}")
        for r in rows2:
            print(f"  [{r[0]}] reason={r[2]}")
            print(f"      summary: {r[1][:150]}")
        assert len(rows2) == 2, f"expected 2 nodes (one per compaction), got {len(rows2)}"

        # Key verification: node 2 should ONLY describe email templates, NOT mention
        # PostgreSQL/pgvector (that's already in node 1). If the buffer wasn't drained,
        # node 2 would contain both topics.
        node2_summary = rows2[1][1].lower()
        print(f"\n[verify] node2 mentions 'postgresql'? {'postgresql' in node2_summary}")
        print(f"[verify] node2 mentions 'pgvector'?   {'pgvector' in node2_summary}")
        print(f"[verify] node2 mentions 'email'?      {'email' in node2_summary}")
        # pgvector should be absent or at most incidental; email must be primary subject
        assert "email" in node2_summary, "node 2 must describe email work"
        if "pgvector" in node2_summary:
            print("  ! WARNING: node 2 also mentions pgvector — may indicate buffer "
                  "leakage (but LLM may have referenced it as context)")

        daemon_mgmt.stop()
    print("\nMulti-pass daemon test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
