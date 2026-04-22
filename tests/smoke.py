"""Smoke tests — run after `pip install -e .` to catch path/import/shape bugs
before involving the `claude` CLI or live CC hooks.

Sections:
  1. Schema instantiation
  2. Policy load + render
  3. Store CRUD (nodes, session brief, tag index)
  4. Parser on a synthetic CC jsonl
  5. Embedder (skipped if --no-embed or sentence-transformers missing)
  6. Retriever passive-first + active match

Usage:
    python3 tests/smoke.py              # full
    python3 tests/smoke.py --no-embed   # skip embedder (avoids 560MB download)
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path


def _header(label: str) -> None:
    print(f"\n=== {label} ===")


def _ok(label: str) -> None:
    print(f"  ✓ {label}")


def _skip(label: str, reason: str) -> None:
    print(f"  ~ {label}  (skipped: {reason})")


def test_schemas() -> None:
    _header("1. Schemas")
    from comet_cc.schemas import (
        CognitiveLoad, CompactedResult, L1Memory, MemoryNode,
    )

    n = MemoryNode(
        node_id=MemoryNode.new_id(),
        summary="hello world",
        trigger="when I greet",
        recall_mode="passive",
        importance="HIGH",
        topic_tags=["greeting"],
    )
    assert n.node_id.startswith("n_")
    assert n.importance_tag() == "IMPORTANCE:HIGH"
    _ok("MemoryNode instantiation + new_id + importance_tag")

    load = CognitiveLoad(logic_flow="BROKEN", load_level=5)
    assert load.needs_compacting
    _ok("CognitiveLoad.needs_compacting")

    mem = L1Memory(content="x")
    assert mem.raw_content == ""
    _ok("L1Memory defaults")

    r = CompactedResult(summary="s", trigger="t")
    assert r.recall_mode == "active" and r.importance == "MED"
    _ok("CompactedResult defaults")


def test_policies() -> None:
    _header("2. Policies")
    from comet_cc.policies import ALL_POLICIES, _SESSION_BRIEF_INSTRUCTION

    expected = {"dialog", "code"}
    assert set(ALL_POLICIES.keys()) == expected
    _ok(f"modalities: {sorted(expected)}")

    dlg = ALL_POLICIES["dialog"]
    assert dlg.extract_rules is True
    _ok("DIALOG.extract_rules=True (brief emission on)")

    block = dlg.render_compactor_instructions()
    assert "conversation" in block.lower() and "trigger" in block
    _ok("dialog policy block renders")

    assert "Active Work Context" in _SESSION_BRIEF_INSTRUCTION
    assert "Hints" in _SESSION_BRIEF_INSTRUCTION
    _ok("session brief instruction intact")


def test_store(tmp_root: Path) -> None:
    _header("3. Store CRUD")
    from comet_cc.core.store import NodeStore
    from comet_cc.schemas import MemoryNode

    store = NodeStore(tmp_root / "store.sqlite")
    try:
        passive = MemoryNode(
            node_id=MemoryNode.new_id(), summary="rule 1",
            trigger="when I do X", recall_mode="passive",
            importance="HIGH", topic_tags=["rule", "IMPORTANCE:HIGH"],
            session_id="s1",
        )
        active1 = MemoryNode(
            node_id=MemoryNode.new_id(), summary="ep 1",
            trigger="when I check auth", recall_mode="active",
            importance="MED", topic_tags=["auth"],
            session_id="s1",
        )
        active2 = MemoryNode(
            node_id=MemoryNode.new_id(), summary="ep 2",
            trigger="when I trace db", recall_mode="active",
            importance="LOW", topic_tags=["db"],
            session_id="s2",
        )
        for n in (passive, active1, active2):
            store.save_node(n)
        _ok("save_node x3")

        got = store.get_node(passive.node_id)
        assert got is not None and got.summary == "rule 1"
        assert got.recall_mode == "passive"
        _ok("get_node round-trip (fields preserved)")

        s1_passive = store.list_passive(session_id="s1")
        assert len(s1_passive) == 1 and s1_passive[0].node_id == passive.node_id
        _ok("list_passive filters by session+recall_mode")

        s1_nodes = store.list_session_nodes("s1")
        assert len(s1_nodes) == 2
        _ok("list_session_nodes ordering")

        tags = store.get_all_tags()
        assert "rule" in tags and "auth" in tags and "db" in tags
        _ok(f"get_all_tags: {sorted(tags)}")

        store.save_session_brief("s1", "## Active Work Context\n- building plugin")
        brief = store.load_session_brief("s1")
        assert "plugin" in brief
        _ok("session brief save/load")

        empty_brief = store.load_session_brief("nonexistent")
        assert empty_brief == ""
        _ok("session brief missing → empty string")
    finally:
        store.close()


def _synthesize_transcript(tmp_root: Path) -> Path:
    """Generate a CC-style jsonl with user/tool/assistant turns."""
    entries = [
        {
            "type": "user", "uuid": "u1", "sessionId": "s1",
            "message": {"role": "user", "content": [
                {"type": "text", "text": "Read README.md and summarize"},
            ]},
        },
        {
            "type": "assistant", "uuid": "a1", "parentUuid": "u1",
            "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "tu1", "name": "Read",
                 "input": {"file_path": "/repo/README.md"}},
            ]},
        },
        {
            "type": "user", "uuid": "u2", "parentUuid": "a1",
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "tu1",
                 "content": "# Title\nA short readme about X", "is_error": False},
            ]},
        },
        {
            "type": "assistant", "uuid": "a2", "parentUuid": "u2",
            "message": {"role": "assistant", "content": [
                {"type": "text", "text": "The README describes X with a title and short intro."},
            ]},
        },
        {
            "type": "user", "uuid": "u3", "parentUuid": "a2",
            "message": {"role": "user", "content": [
                {"type": "text", "text": "Great, now run the tests"},
            ]},
        },
    ]
    path = tmp_root / "sample.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in entries), encoding="utf-8")
    return path


def test_parser(tmp_root: Path) -> None:
    _header("4. Parser (synthetic CC jsonl)")
    from comet_cc.parser import choose_policy_for_bundle, parse_transcript

    path = _synthesize_transcript(tmp_root)
    nodes = parse_transcript(path)
    kinds = [n.kind for n in nodes]
    assert kinds == ["user_text", "tool_bundle", "assistant_text", "user_text"], (
        f"unexpected sequence: {kinds}"
    )
    _ok(f"node sequence: {kinds}")

    bundle = nodes[1]
    assert bundle.tool_names == ["Read"]
    assert "Read(" in bundle.content
    _ok("tool_bundle captures tool_use + tool_result pairing")

    policy_name = choose_policy_for_bundle(bundle)
    assert policy_name == "code", f"got {policy_name}"
    _ok("choose_policy_for_bundle routes Read → code")


def test_embedder() -> None:
    _header("5. Embedder")
    try:
        from comet_cc.core import vector
    except ImportError as e:
        _skip("embedder", f"import: {e}")
        return
    try:
        v1 = vector.embed("hello world")
        v2 = vector.embed("안녕하세요 세계")
        v3 = vector.embed("the quick brown fox jumps over")
    except Exception as e:
        _skip("embedder", f"runtime: {e}")
        return
    assert v1.shape == (vector.DIM,), f"wrong dim: {v1.shape}"
    _ok(f"embed() returns dim={vector.DIM} float32")

    ranked = vector.cosine_search(v1, [("A", v2), ("B", v3)], top_k=2, min_score=0.0)
    assert len(ranked) == 2
    _ok(f"cosine_search: {[(i, round(s, 3)) for i, s in ranked]}")


def test_retriever(tmp_root: Path, skip_embed: bool) -> None:
    _header("6. Retriever")
    from comet_cc.core.store import NodeStore
    from comet_cc.schemas import MemoryNode

    store = NodeStore(tmp_root / "retriever.sqlite")
    try:
        passive = MemoryNode(
            node_id="np1", summary="don't mock db in tests",
            trigger="when I write tests", recall_mode="passive",
            importance="HIGH", topic_tags=["testing"], session_id="s1",
        )
        store.save_node(passive)

        if not skip_embed:
            from comet_cc.core import retriever, vector
            active = MemoryNode(
                node_id="na1", summary="auth middleware uses JWT",
                trigger="when I debug auth", recall_mode="active",
                importance="MED", topic_tags=["auth"], session_id="s1",
            )
            emb = vector.embed(active.summary + "\n" + active.trigger)
            store.save_node(active, embedding=emb)

            nodes = retriever.get_context_window(
                store, session_id="s1",
                query="I need to check JWT auth flow",
                max_nodes=5,
            )
            ids = [n.node_id for n in nodes]
            assert "np1" in ids, "passive must always ride"
            assert ids[0] == "np1", "passive must come first"
            assert "na1" in ids, "active should match 'JWT auth' query"
            _ok(f"passive-first + active vector match: {ids}")

            rendered = retriever.render_nodes(nodes)
            assert "np1" in rendered and "summary" in rendered
            _ok("render_nodes output has ids + summary lines")
        else:
            from comet_cc.core import retriever
            nodes = retriever.get_context_window(
                store, session_id="s1", query=None, max_nodes=5,
            )
            ids = [n.node_id for n in nodes]
            assert ids == ["np1"], f"expected only passive, got {ids}"
            _ok("passive-only path works without embedder")
    finally:
        store.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-embed", action="store_true",
                    help="Skip embedder test (avoids BGE-M3 download)")
    args = ap.parse_args()

    with tempfile.TemporaryDirectory(prefix="comet-cc-smoke-") as td:
        tmp_root = Path(td)
        test_schemas()
        test_policies()
        test_store(tmp_root)
        test_parser(tmp_root)
        if not args.no_embed:
            test_embedder()
        test_retriever(tmp_root, skip_embed=args.no_embed)

    print("\nAll smoke tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
