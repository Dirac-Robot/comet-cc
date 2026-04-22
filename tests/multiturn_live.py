"""Headless multi-turn live test — drives a real Claude Code session through
several prompts with a topic pivot, then asserts the proxy's behavior.

Pins to a single session via explicit `--resume <session_id>` (NOT `-c`),
since `-c` would resolve to "most recent session in cwd" and the sensor/
compacter `claude -p` subprocesses land their own jsonls in the shared
project dir.

The proxy env is applied via `comet-cc run` which execs the inner `claude`
with ANTHROPIC_BASE_URL + NODE_EXTRA_CA_CERTS pre-set.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path


PY = "/Users/vanta/miniconda3/bin/python"
HOME = Path.home() / ".comet-cc"
PROJECT = Path("/tmp/cometcc-multiturn")


def reset() -> None:
    subprocess.run([PY, "-m", "comet_cc.cli", "daemon", "stop"], capture_output=True)
    time.sleep(1)
    if HOME.exists():
        shutil.rmtree(HOME)
    if PROJECT.exists():
        shutil.rmtree(PROJECT)
    PROJECT.mkdir(parents=True)
    # Blow away prior CC jsonls for this path so --resume never picks stale.
    proj_hash = Path.home() / ".claude" / "projects" / "-private-tmp-cometcc-multiturn"
    if proj_hash.exists():
        shutil.rmtree(proj_hash)
    # Tighten the buffer ceiling so the test reliably trips the
    # `buffer_overflow` compaction path within 7 turns, independent of
    # the haiku sensor's non-deterministic topic-shift judgement.
    env_overrides = os.environ.copy()
    env_overrides["COMET_CC_MAX_L1"] = "5"
    # Generate CA + start the daemon (proxy comes up on port 8443).
    subprocess.run(
        [PY, "-m", "comet_cc.cli", "install"],
        capture_output=True, env=env_overrides,
    )
    subprocess.run(
        [PY, "-m", "comet_cc.cli", "daemon", "start"],
        check=True, env=env_overrides,
    )
    # Give the daemon a moment to finish warming BGE-M3 and bind the port.
    time.sleep(5)
    print(f"Fresh project: {PROJECT}  (COMET_CC_MAX_L1=5 for deterministic compact)")


def _invoke(args: list[str], timeout: int = 300) -> dict:
    """Wraps every claude invocation in `comet-cc run` to pick up proxy env."""
    full = [PY, "-m", "comet_cc.cli", "run"] + args
    proc = subprocess.run(
        full, cwd=str(PROJECT), capture_output=True, text=True, timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"claude rc={proc.returncode} stderr={proc.stderr[:300]}"
        )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"non-JSON envelope: {proc.stdout[:300]}") from e


def first_turn(prompt: str) -> tuple[str, str]:
    env = _invoke([
        "claude", "-p", prompt, "--output-format", "json", "--model", "sonnet",
    ])
    return env["session_id"], env.get("result", "")


def continue_turn(session_id: str, prompt: str) -> str:
    env = _invoke([
        "claude", "--resume", session_id,
        "-p", prompt, "--output-format", "json", "--model", "sonnet",
    ])
    return env.get("result", "")


def main() -> int:
    reset()

    turns = [
        "Python으로 FizzBuzz 함수 짜줘. 코드만, 설명은 짧게.",
        "이 코드를 클래스로 감싸줄 수 있어?",
        "완전 다른 얘기인데, 김치찌개 레시피 간단하게 알려줘.",
        "국물 색깔이 진해지는 비결이 뭐야?",
        "또 다른 토픽 — 토성의 위성 중에 가장 큰 건 뭐야?",
        "그 위성 대기 성분은?",
        "아까 맨 처음에 Python으로 뭐 짜달라고 했지? 정확하게 답해줘.",
    ]

    print(f"\n--- Turn 1 (fresh) ---")
    print(f"> {turns[0]}")
    session_id, out = first_turn(turns[0])
    print(f"  session_id = {session_id}")
    print(f"< {out[:200]}")
    time.sleep(3)

    for i, t in enumerate(turns[1:], start=2):
        print(f"\n--- Turn {i} (resume {session_id[:8]}) ---")
        print(f"> {t}")
        out = continue_turn(session_id, t)
        print(f"< {out[:200]}")
        time.sleep(3)

    print("\n=== Waiting for daemon trim queue to drain ===")
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from comet_cc import client
    deadline = time.monotonic() + 240
    idle_streak = 0
    while time.monotonic() < deadline:
        resp = client.queue_depth(timeout=2.0)
        if resp and resp.get("ok"):
            q = resp.get("queue_size", 0)
            a = resp.get("active", 0)
            print(f"  queue={q} active={a}")
            if q == 0 and a == 0:
                idle_streak += 1
                if idle_streak >= 2:
                    break
            else:
                idle_streak = 0
        time.sleep(3)
    else:
        print("  ! drain timeout reached")

    # ------------------- Assertions -------------------
    failures: list[str] = []

    print("\n=== Daemon log summary ===")
    log = HOME / "logs" / "daemon.log"
    assert log.exists(), "daemon log missing"
    lines = log.read_text().splitlines()
    counts = {
        tag: sum(1 for l in lines if tag in l)
        for tag in ["queued sensor_check", "sensor[", "compact[",
                    "rewrote messages", "injected retrieval"]
    }
    for tag, n in counts.items():
        print(f"  {tag:22s} {n:3d}")

    if counts["queued sensor_check"] < 1:
        failures.append(
            f"sensor never queued — trim orchestrator may not be wired "
            f"(got {counts['queued sensor_check']})"
        )
    if counts["injected retrieval"] < 1:
        failures.append(
            f"retrieval never injected — expected ≥ 1 after first compact "
            f"(got {counts['injected retrieval']})"
        )

    our_lines = [l for l in lines if session_id[:8] in l]
    print(f"\n  lines mentioning our session {session_id[:8]}: {len(our_lines)}")
    for l in our_lines[-20:]:
        print(f"    {l.split('|', 2)[-1].strip()[:140]}")

    print("\n=== Store contents ===")
    db = HOME / "store.sqlite"
    assert db.exists(), "store.sqlite missing"
    conn = sqlite3.connect(str(db))
    rows = conn.execute(
        "SELECT node_id, session_id, recall_mode, importance, "
        "compaction_reason, summary FROM nodes ORDER BY created_at"
    ).fetchall()
    print(f"  nodes: {len(rows)}")
    for r in rows:
        print(f"    [{r[0]}] session={r[1][:8] if r[1] else 'None'} "
              f"imp={r[3]} reason={r[4]}")
        print(f"        summary: {r[5][:180]}")

    briefs = conn.execute(
        "SELECT session_id, LENGTH(brief), brief FROM session_briefs"
    ).fetchall()
    print(f"\n  session briefs: {len(briefs)}")
    for r in briefs:
        print(f"    [{r[0][:8]}] len={r[1]}")
    conn.close()

    our_nodes = [r for r in rows if r[1] == session_id]
    print(f"\n  nodes for OUR session ({session_id[:8]}): {len(our_nodes)}")
    if len(our_nodes) < 1:
        failures.append(
            f"Expected ≥ 1 node for our session (sensor should trip once "
            f"within 7 turns spanning 3 topics); got {len(our_nodes)}"
        )

    # Topic coverage is probabilistic — the LLM sonnet sometimes refuses
    # off-domain prompts (e.g., cooking, astronomy) when it's been primed
    # as a coding assistant, leaving no substance to summarize. Accept if
    # ANY of the 3 expected topics surfaces in SOME stored node.
    all_summary_text = " ".join(r[5].lower() for r in our_nodes)
    expected_topics = [
        ("fizzbuzz", ["fizzbuzz", "fizz"]),
        ("kimchi", ["김치", "찌개", "레시피"]),
        ("saturn", ["토성", "위성", "타이탄"]),
    ]
    hits = []
    for label, keywords in expected_topics:
        hit = any(kw in all_summary_text for kw in keywords)
        print(f"  topic '{label}' captured: {hit} (searched: {keywords})")
        if hit:
            hits.append(label)
    if not hits:
        failures.append(
            "No expected topic surfaced in any stored node — compacter "
            "may be producing empty/unrelated summaries"
        )

    # Contamination check — compacter subprocess strips proxy env, so its
    # `claude -p` traffic should go straight to Anthropic and never land as
    # stored nodes talking about our own internals.
    zombie_markers = [
        "cognitive load analyzer", "cognitive-load-analyzer",
        "memory indexer", "logic_flow=",
    ]
    contaminated = sum(
        1 for r in rows
        if any(m.lower() in r[5].lower() for m in zombie_markers)
    )
    print(f"  nodes contaminated by sensor/compacter artifacts: {contaminated}")
    if contaminated > 0:
        failures.append(
            f"{contaminated} node(s) polluted by plugin-internal chatter — "
            "env-strip guard not working"
        )

    # Stop the daemon to free the port for subsequent runs.
    subprocess.run([PY, "-m", "comet_cc.cli", "daemon", "stop"],
                   capture_output=True)

    print("\n=== Verdict ===")
    if failures:
        print(f"FAIL ({len(failures)} issue(s)):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS — proxy-mode multi-turn end-to-end working")
    return 0


if __name__ == "__main__":
    sys.exit(main())
