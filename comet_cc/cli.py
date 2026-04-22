"""`comet-cc` CLI — install/uninstall hooks + skill, daemon lifecycle,
and memory operations for Skill-driven recall (search/read-node/list-session).
"""

from __future__ import annotations

import argparse
import json
import shlex
import shutil
import sys
from pathlib import Path

from loguru import logger

from comet_cc import client, config, daemon_mgmt

_CLAUDE_HOME = Path.home() / ".claude"
_CLAUDE_SETTINGS = _CLAUDE_HOME / "settings.json"
_CLAUDE_SKILLS_DIR = _CLAUDE_HOME / "skills"
_SKILL_NAME = "comet-cc-memory"
_PACKAGE_SKILL_DIR = Path(__file__).parent.parent / "skills" / _SKILL_NAME


def _hook_commands() -> dict[str, str]:
    py = shlex.quote(sys.executable)
    return {
        "SessionStart": f"{py} -m comet_cc.hooks.session_start",
        "UserPromptSubmit": f"{py} -m comet_cc.hooks.user_prompt",
        "Stop": f"{py} -m comet_cc.hooks.stop",
        "PreCompact": f"{py} -m comet_cc.hooks.pre_compact",
    }


_HOOK_MODULES = {
    "SessionStart": "comet_cc.hooks.session_start",
    "UserPromptSubmit": "comet_cc.hooks.user_prompt",
    "Stop": "comet_cc.hooks.stop",
    "PreCompact": "comet_cc.hooks.pre_compact",
}


# ---------- settings.json I/O ----------


def _load_settings() -> dict:
    if _CLAUDE_SETTINGS.exists():
        try:
            return json.loads(_CLAUDE_SETTINGS.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.error(f"{_CLAUDE_SETTINGS} is not valid JSON — aborting")
            sys.exit(1)
    return {}


def _write_settings(data: dict) -> None:
    _CLAUDE_SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    _CLAUDE_SETTINGS.write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8",
    )


def _command_targets_module(cmd: str, module: str) -> bool:
    return f"-m {module}" in cmd or module in cmd.split()


def _install_hooks(settings: dict) -> dict:
    hooks = settings.setdefault("hooks", {})
    commands = _hook_commands()
    for event, command in commands.items():
        module = _HOOK_MODULES[event]
        bucket = hooks.setdefault(event, [])
        for matcher in bucket:
            matcher["hooks"] = [
                h for h in matcher.get("hooks", [])
                if not _command_targets_module(h.get("command", ""), module)
            ]
        bucket[:] = [m for m in bucket if m.get("hooks")]
        bucket.append({
            "matcher": "*",
            "hooks": [{"type": "command", "command": command}],
        })
    return settings


def _uninstall_hooks(settings: dict) -> dict:
    hooks = settings.get("hooks", {})
    for event, module in _HOOK_MODULES.items():
        bucket = hooks.get(event, [])
        for matcher in bucket:
            matcher["hooks"] = [
                h for h in matcher.get("hooks", [])
                if not _command_targets_module(h.get("command", ""), module)
            ]
        hooks[event] = [m for m in bucket if m.get("hooks")]
        if not hooks[event]:
            hooks.pop(event, None)
    return settings


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
    settings = _install_hooks(_load_settings())
    _write_settings(settings)
    skill_dest = _install_skill()
    config.home()
    print(f"Installed hooks → {_CLAUDE_SETTINGS}")
    if skill_dest:
        print(f"Installed skill → {skill_dest}")
    print(f"Home: {config.home()}")
    print("Run `comet-cc daemon start` to preload the embedder now, or let")
    print("SessionStart auto-spawn it on the first Claude Code session.")


def cmd_uninstall(_args) -> None:
    settings = _uninstall_hooks(_load_settings())
    _write_settings(settings)
    _uninstall_skill()
    print(f"Removed hooks from {_CLAUDE_SETTINGS}")
    print(f"Removed skill from {_CLAUDE_SKILLS_DIR / _SKILL_NAME}")


def cmd_status(_args) -> None:
    settings = _load_settings()
    hooks = settings.get("hooks", {})
    print("Hooks:")
    for event, module in _HOOK_MODULES.items():
        installed_cmd = None
        for matcher in hooks.get(event, []):
            for h in matcher.get("hooks", []):
                if _command_targets_module(h.get("command", ""), module):
                    installed_cmd = h.get("command")
                    break
            if installed_cmd:
                break
        marker = "✓" if installed_cmd else "✗"
        shown = installed_cmd or f"(not installed — target: {_hook_commands()[event]})"
        print(f"  {marker} {event:18s} {shown}")

    skill_dest = _CLAUDE_SKILLS_DIR / _SKILL_NAME
    skill_mark = "✓" if skill_dest.exists() else "✗"
    print(f"\nSkill:\n  {skill_mark} {skill_dest}")

    print("\nDaemon:")
    running = daemon_mgmt.is_running()
    pid = daemon_mgmt.read_pid()
    print(f"  {'✓' if running else '✗'} running={running} pid={pid} "
          f"socket={config.daemon_socket()}")

    print(f"\nHome: {config.home()}")
    print(f"Store: {config.store_path()}")


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
    resp = client.get_node(args.node_id)
    if not resp or not resp.get("ok"):
        if resp is None:
            print("daemon not reachable — start it with `comet-cc daemon start`")
            sys.exit(2)
        print(f"node {args.node_id} not found")
        sys.exit(1)
    print(_format_node(resp["node"], full=True))


def cmd_list_session(args) -> None:
    resp = client.list_session_nodes(args.session)
    if not resp or not resp.get("ok"):
        print("daemon not reachable — start it with `comet-cc daemon start`")
        sys.exit(2)
    nodes = resp.get("nodes", [])
    if not nodes:
        print("(no nodes for this session)")
        return
    print(f"# Session {args.session} — {len(nodes)} nodes")
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

    sub.add_parser("install")
    sub.add_parser("uninstall")
    sub.add_parser("status")

    d = sub.add_parser("daemon")
    d.add_argument("action", choices=["start", "stop", "status"])

    s = sub.add_parser("search", help="Semantic search across stored memory")
    s.add_argument("query")
    s.add_argument("--session", default=None, help="Scope to a session id")
    s.add_argument("--top", type=int, default=5)
    s.add_argument("--min-score", type=float, default=0.30)
    s.add_argument("--no-brief", action="store_true")

    r = sub.add_parser("read-node", help="Read a specific node by id")
    r.add_argument("node_id")
    r.add_argument("--session", default=None)

    ls = sub.add_parser("list-session", help="List all nodes in a session")
    ls.add_argument("session")

    b = sub.add_parser("brief", help="Show this session's buttered brief")
    b.add_argument("session")

    args = parser.parse_args()
    {
        "install": cmd_install,
        "uninstall": cmd_uninstall,
        "status": cmd_status,
        "daemon": cmd_daemon,
        "search": cmd_search,
        "read-node": cmd_read_node,
        "list-session": cmd_list_session,
        "brief": cmd_brief,
    }[args.cmd](args)


if __name__ == "__main__":
    main()
