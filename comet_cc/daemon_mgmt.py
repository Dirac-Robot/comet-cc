"""Daemon lifecycle helpers — start/stop/status and auto-spawn from hooks."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from loguru import logger

from comet_cc import client, config


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_pid() -> int | None:
    p = config.daemon_pid_file()
    if not p.exists():
        return None
    try:
        return int(p.read_text(encoding="utf-8").strip())
    except ValueError:
        return None


def is_running() -> bool:
    """Daemon is considered running iff PID file exists, process is alive,
    AND the socket responds to ping. This avoids zombies / stale PIDs."""
    pid = read_pid()
    if pid is None or not _pid_alive(pid):
        return False
    return client.ping(timeout=1.0)


def spawn_detached() -> int:
    """Spawn the daemon as a detached process. Returns its PID."""
    log = config.daemon_log().open("ab")
    proc = subprocess.Popen(
        [sys.executable, "-m", "comet_cc.daemon"],
        stdin=subprocess.DEVNULL, stdout=log, stderr=log,
        start_new_session=True, close_fds=True,
    )
    return proc.pid


def ensure_running(wait_seconds: float = 15.0) -> bool:
    """Spawn the daemon if not alive. Block until it responds to ping,
    with a timeout. Returns True iff the daemon is ready."""
    if is_running():
        return True

    stale = config.daemon_pid_file()
    if stale.exists():
        pid = read_pid()
        if pid is None or not _pid_alive(pid):
            try:
                stale.unlink()
            except OSError:
                pass

    sock = config.daemon_socket()
    if sock.exists():
        try:
            sock.unlink()
        except OSError:
            pass

    pid = spawn_detached()
    logger.info(f"spawned daemon pid={pid}")

    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if client.ping(timeout=0.5):
            return True
        time.sleep(0.3)
    return False


def stop() -> bool:
    pid = read_pid()
    if pid is None:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return False
    for _ in range(30):
        if not _pid_alive(pid):
            return True
        time.sleep(0.2)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    return True
