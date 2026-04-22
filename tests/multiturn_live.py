"""Headless multi-turn live test — drives a real Claude Code session through
several prompts with a topic pivot, then asserts the plugin's behavior.

Pins to a single session via explicit `--resume <session_id>` (NOT `-c`),
because `-c` resolves to "most recent session in cwd" which the plugin's
own sensor/compacter `claude -p` subprocesses keep contaminating.
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

    claude_dir = PROJECT / ".claude"
    (claude_dir / "skills").mkdir(parents=True)
    shutil.copytree(
        Path(__file__).parent.parent / "skills" / "comet-cc-memory",
        claude_dir / "skills" / "comet-cc-memory",
    )
    settings = {
        "hooks": {
            event: [{
                "matcher": "*",
                "hooks": [{"type": "command",
                           "command": f"{PY} -m comet_cc.hooks.{module}"}],
            }]
            for event, module in [
                ("SessionStart", "session_start"),
                ("UserPromptSubmit", "user_prompt"),
                ("Stop", "stop"),
                ("PreCompact", "pre_compact"),
            ]
        },
    }
    (claude_dir / "settings.json").write_text(
        json.dumps(settings, indent=2), encoding="utf-8",
    )
    # Blow away prior CC jsonls for this path so -c can never resolve to stale
    proj_hash = Path.home() / ".claude" / "projects" / "-private-tmp-cometcc-multiturn"
    if proj_hash.exists():
        shutil.rmtree(proj_hash)
    print(f"Fresh project: {PROJECT}")


def _invoke(args: list[str], timeout: int = 300) -> dict:
    proc = subprocess.run(
        args, cwd=str(PROJECT), capture_output=True, text=True, timeout=timeout,
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

    print("\n=== Waiting for daemon compact queue to drain ===")
    # Poll daemon queue depth until empty + no active job for 2 cycles,
    # with an absolute cap so a hung compacter doesn't wedge the test.
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

    print("\n=== Hook log summary ===")
    log = HOME / "logs" / "hook.log"
    assert log.exists(), "hook log missing"
    lines = log.read_text().splitlines()
    counts = {
        evt: sum(1 for l in lines if f"[{evt}]" in l)
        for evt in ["SessionStart", "UserPromptSubmit", "Stop", "PreCompact"]
    }
    for evt, n in counts.items():
        print(f"  {evt:18s} {n:3d} events")

    if counts["UserPromptSubmit"] < len(turns):
        failures.append(
            f"UserPromptSubmit fired {counts['UserPromptSubmit']} times, "
            f"expected ≥ {len(turns)}"
        )
    if counts["Stop"] < len(turns):
        failures.append(
            f"Stop fired {counts['Stop']} times, expected ≥ {len(turns)}"
        )

    our_session_lines = [l for l in lines if session_id in l]
    print(f"\n  lines mentioning our session {session_id[:8]}: {len(our_session_lines)}")
    for l in our_session_lines[-20:]:
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

    # Collect node summaries for our session only.
    our_nodes = [r for r in rows if r[1] == session_id]
    print(f"\n  nodes for OUR session ({session_id[:8]}): {len(our_nodes)}")

    if len(our_nodes) < 2:
        failures.append(
            f"Expected ≥ 2 nodes for our session (3 topics: code / 요리 / 천문); "
            f"got {len(our_nodes)}"
        )

    all_summary_text = " ".join(r[5].lower() for r in our_nodes)
    expected_topics = [
        ("fizzbuzz", ["fizzbuzz", "fizz"]),
        ("kimchi", ["김치", "찌개", "레시피"]),
        ("saturn", ["토성", "위성", "타이탄"]),
    ]
    for label, keywords in expected_topics:
        hit = any(kw in all_summary_text for kw in keywords)
        print(f"  topic '{label}' captured: {hit} (searched: {keywords})")
        if not hit:
            failures.append(f"Topic '{label}' not captured in any node summary")

    # UserPromptSubmit injection: at least one turn on our session should
    # show nodes>=1 OR brief=True (after first compact saved to store).
    injection_events = [
        l for l in lines
        if "UserPromptSubmit" in l and session_id in l
    ]
    has_injection = any(
        ("nodes=" in l and "nodes=0" not in l)
        or "brief=True" in l
        for l in injection_events
    )
    print(f"\n  at least one UserPromptSubmit on our session had nodes>=1 "
          f"or brief=True: {has_injection}")
    if not has_injection:
        failures.append(
            "No UserPromptSubmit turn on our session received non-empty "
            "retrieval (push-injection path never fired)"
        )

    # No zombie contamination: node summaries should NOT be dominated by
    # 'cognitive-load-analyzer' / 'memory indexer' self-references.
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
            "env guard not working"
        )

    print("\n=== jsonl count ===")
    proj_dir = Path.home() / ".claude" / "projects" / "-private-tmp-cometcc-multiturn"
    real_session_jsonl = 0
    if proj_dir.exists():
        files = list(proj_dir.glob("*.jsonl"))
        sizes = [(f.name, sum(1 for _ in open(f))) for f in files]
        sizes.sort(key=lambda x: x[1], reverse=True)
        print(f"  total: {len(files)}")
        for name, n in sizes[:6]:
            marker = " ← our session" if name.startswith(session_id) else ""
            print(f"    {name}: {n} lines{marker}")
        real_session_jsonl = sum(1 for _, n in sizes if n >= 20)

    subprocess.run([PY, "-m", "comet_cc.cli", "daemon", "stop"],
                   capture_output=True)

    print("\n=== Verdict ===")
    if failures:
        print(f"FAIL ({len(failures)} issue(s)):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS — multi-turn end-to-end working")
    return 0


if __name__ == "__main__":
    sys.exit(main())
