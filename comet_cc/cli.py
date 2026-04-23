"""`comet-cc` CLI — install (cert + skill), daemon lifecycle, `run` launcher
to wrap Claude Code with proxy env vars, and memory operations for Skill-driven
recall (search/read-node/list-session).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

from loguru import logger

from comet_cc import client, config, daemon_mgmt
from comet_cc.proxy import cert as cert_module

_CLAUDE_HOME = Path.home() / ".claude"
_CLAUDE_SKILLS_DIR = _CLAUDE_HOME / "skills"
_SKILL_NAME = "comet-cc-memory"
_PACKAGE_SKILL_DIR = Path(__file__).parent.parent / "skills" / _SKILL_NAME


# ---------- skill install ----------


def _install_skill() -> Path | None:
    if not _PACKAGE_SKILL_DIR.exists():
        print(f"  ! skill source not found: {_PACKAGE_SKILL_DIR}")
        return None
    dest = _CLAUDE_SKILLS_DIR / _SKILL_NAME
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(_PACKAGE_SKILL_DIR, dest)
    return dest


def _uninstall_skill() -> None:
    dest = _CLAUDE_SKILLS_DIR / _SKILL_NAME
    if dest.exists():
        shutil.rmtree(dest)


# ---------- command handlers ----------


def cmd_install(_args) -> None:
    paths = cert_module.ensure_certs()
    skill_dest = _install_skill()
    config.home()
    print(f"Home: {config.home()}")
    print(f"CA cert: {paths['ca']}")
    if skill_dest:
        print(f"Skill : {skill_dest}")
    print()
    print("Next:")
    print(f"  1. Start the daemon:   comet-cc daemon start")
    print(f"  2. Launch CC through the proxy:")
    print(f"       comet-cc run claude [args...]")
    print(f"     (or set ANTHROPIC_BASE_URL + NODE_EXTRA_CA_CERTS manually; "
          f"see `comet-cc env`).")


def cmd_uninstall(_args) -> None:
    _uninstall_skill()
    print(f"Removed skill from {_CLAUDE_SKILLS_DIR / _SKILL_NAME}")
    print(f"Note: certs + store kept at {config.home()}. "
          f"Delete the directory manually if you want a full wipe.")


def cmd_status(_args) -> None:
    paths = config.cert_dir()
    ca = paths / "ca.crt"
    server_pem = paths / "server.pem"
    print("Cert:")
    for label, p in [("CA", ca), ("leaf bundle", server_pem)]:
        mark = "✓" if p.exists() else "✗"
        print(f"  {mark} {label}: {p}")

    skill_dest = _CLAUDE_SKILLS_DIR / _SKILL_NAME
    skill_mark = "✓" if skill_dest.exists() else "✗"
    print(f"\nSkill:\n  {skill_mark} {skill_dest}")

    print("\nDaemon:")
    running = daemon_mgmt.is_running()
    pid = daemon_mgmt.read_pid()
    print(f"  {'✓' if running else '✗'} running={running} pid={pid} "
          f"socket={config.daemon_socket()}")
    print(f"  proxy: https://{config.PROXY_HOST}:{config.PROXY_PORT} "
          f"-> {config.UPSTREAM_URL}")

    print(f"\nHome: {config.home()}")
    print(f"Store: {config.store_path()}")


def cmd_env(_args) -> None:
    """Print shell exports for manual wrapping (non-interactive use)."""
    paths = cert_module.ensure_certs()
    print(f'export ANTHROPIC_BASE_URL="https://{config.PROXY_HOST}:{config.PROXY_PORT}"')
    print(f'export NODE_EXTRA_CA_CERTS="{paths["ca"]}"')


def cmd_run(args) -> None:
    """Exec an arbitrary command with proxy env vars pre-set. Typical:
       comet-cc run claude -p 'hi' --model sonnet
    """
    argv = args.argv
    if not argv:
        print("usage: comet-cc run <command> [args...]", file=sys.stderr)
        sys.exit(2)
    paths = cert_module.ensure_certs()
    env = {
        **os.environ,
        "ANTHROPIC_BASE_URL": f"https://{config.PROXY_HOST}:{config.PROXY_PORT}",
        "NODE_EXTRA_CA_CERTS": str(paths["ca"]),
    }
    # Give the daemon a chance to boot opportunistically so first request
    # doesn't race against a cold listener.
    if not daemon_mgmt.is_running():
        daemon_mgmt.ensure_running(wait_seconds=20.0)
    try:
        os.execvpe(argv[0], argv, env)
    except FileNotFoundError:
        print(f"command not found: {argv[0]}", file=sys.stderr)
        sys.exit(127)


def cmd_daemon(args) -> None:
    if args.action == "start":
        if daemon_mgmt.is_running():
            print("Daemon already running.")
            return
        ok = daemon_mgmt.ensure_running(wait_seconds=20.0)
        print("Started." if ok else "Daemon did not become ready in time.")
        sys.exit(0 if ok else 1)
    if args.action == "stop":
        ok = daemon_mgmt.stop()
        print("Stopped." if ok else "Daemon was not running.")
        return
    if args.action == "status":
        running = daemon_mgmt.is_running()
        pid = daemon_mgmt.read_pid()
        print(f"running={running} pid={pid} socket={config.daemon_socket()}")
        sys.exit(0 if running else 1)


# ---------- memory subcommands (skill calls these) ----------


def _format_node(node_dict: dict, full: bool = False) -> str:
    tags = [t for t in node_dict.get("topic_tags", []) if not t.startswith("IMPORTANCE:")]
    head = (
        f"[{node_dict['node_id']}] recall={node_dict.get('recall_mode', 'active')} "
        f"imp={node_dict.get('importance', 'MED')}"
    )
    if tags:
        head += f" tags={','.join(tags)}"
    if node_dict.get("session_id"):
        head += f" session={node_dict['session_id']}"
    lines = [head, f"  summary: {node_dict.get('summary', '')}"]
    if node_dict.get("trigger"):
        lines.append(f"  trigger: {node_dict['trigger']}")
    if full and node_dict.get("compaction_reason"):
        lines.append(f"  reason : {node_dict['compaction_reason']}")
    return "\n".join(lines)


def cmd_search(args) -> None:
    resp = client.get_context_window(
        session_id=args.session, query=args.query,
        max_nodes=args.top, min_score=args.min_score, timeout=15.0,
    )
    if not resp or not resp.get("ok"):
        print("daemon not reachable — start it with `comet-cc daemon start`")
        sys.exit(2)
    nodes = resp.get("nodes", [])
    brief = resp.get("brief", "")
    if brief.strip() and not args.no_brief:
        print("# Session Brief")
        print(brief.strip())
        print()
    if not nodes:
        print("(no matches)")
        return
    print(f"# Matches ({len(nodes)})")
    for n in nodes:
        print(_format_node(n))


def cmd_read_node(args) -> None:
    resp = client.read_memory(args.node_id, depth=args.depth)
    if not resp or not resp.get("ok"):
        if resp is None:
            print("daemon not reachable — start it with `comet-cc daemon start`")
            sys.exit(2)
        print(f"node {args.node_id} not found")
        sys.exit(1)
    node = resp["node"]
    depth = resp.get("depth", 0)
    print(_format_node(node, full=True))
    if depth == 1:
        cached = " (cached)" if resp.get("cached") else " (generated)"
        print(f"\n# Detailed summary{cached}")
        print(resp.get("text", ""))
        if resp.get("note"):
            print(f"\nnote: {resp['note']}")
    elif depth == 2:
        turns = resp.get("turns", [])
        print(f"\n# Raw turns ({len(turns)})")
        for pos, role, text in turns:
            print(f"\n[{pos}] {role}:")
            print(text)

    if args.links:
        linked = client.list_linked_nodes(args.node_id)
        nodes = (linked or {}).get("nodes") or []
        print(f"\n# Linked children ({len(nodes)})")
        if not nodes:
            print("(none)")
        for n in nodes:
            print(_format_node(n, full=False))


def cmd_list_session(args) -> None:
    resp = client.list_session_nodes(args.session, include_children=args.all)
    if not resp or not resp.get("ok"):
        print("daemon not reachable — start it with `comet-cc daemon start`")
        sys.exit(2)
    nodes = resp.get("nodes", [])
    if not nodes:
        print("(no nodes for this session)")
        return
    scope = " (including children)" if args.all else ""
    print(f"# Session {args.session} — {len(nodes)} nodes{scope}")
    for n in nodes:
        print(_format_node(n))


def cmd_brief(args) -> None:
    resp = client.load_session_brief(args.session)
    if not resp or not resp.get("ok"):
        print("daemon not reachable")
        sys.exit(2)
    brief = resp.get("brief", "")
    if brief.strip():
        print(brief)
    else:
        print("(no brief yet for this session)")


# ---------- argparse wiring ----------


def main() -> None:
    parser = argparse.ArgumentParser(prog="comet-cc")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("install", help="Generate CA cert + install skill")
    sub.add_parser("uninstall", help="Remove skill (cert + store kept)")
    sub.add_parser("status", help="Show cert, skill, daemon, proxy status")
    sub.add_parser("env", help="Print shell exports for ANTHROPIC_BASE_URL + NODE_EXTRA_CA_CERTS")

    d = sub.add_parser("daemon", help="Start/stop/status the background daemon + proxy")
    d.add_argument("action", choices=["start", "stop", "status"])

    r = sub.add_parser("run", help="Exec a command with proxy env vars pre-set (typically: comet-cc run claude ...)")
    r.add_argument("argv", nargs=argparse.REMAINDER)

    s = sub.add_parser("search", help="Semantic search across stored memory")
    s.add_argument("query")
    s.add_argument("--session", default=None, help="Scope to a session id")
    s.add_argument("--top", type=int, default=5)
    s.add_argument("--min-score", type=float, default=0.30)
    s.add_argument("--no-brief", action="store_true")

    rn = sub.add_parser("read-node", help="Read a node at a given depth (0=summary, 1=detailed lazy-gen, 2=raw turns)")
    rn.add_argument("node_id")
    rn.add_argument("--session", default=None)
    rn.add_argument("--depth", type=int, default=0, choices=[0, 1, 2],
                    help="0 summary (default), 1 detailed summary, 2 raw turn data")
    rn.add_argument("--links", action="store_true",
                    help="Also list child nodes linked to this node")

    ls = sub.add_parser("list-session", help="List all nodes in a session (parents only by default)")
    ls.add_argument("session")
    ls.add_argument("--all", action="store_true",
                    help="Include child nodes (drill-down leaves) in the listing")

    b = sub.add_parser("brief", help="Show this session's rolling brief")
    b.add_argument("session")

    args = parser.parse_args()
    {
        "install": cmd_install,
        "uninstall": cmd_uninstall,
        "status": cmd_status,
        "env": cmd_env,
        "daemon": cmd_daemon,
        "run": cmd_run,
        "search": cmd_search,
        "read-node": cmd_read_node,
        "list-session": cmd_list_session,
        "brief": cmd_brief,
    }[args.cmd](args)


if __name__ == "__main__":
    main()
