"""Runtime paths + tunables for the CoMeT-CC plugin.

All paths live under $COMET_CC_HOME (default ~/.comet-cc/) so CC sessions
from any project share a single memory store.
"""

from __future__ import annotations

import os
from pathlib import Path


def home() -> Path:
    p = Path(os.environ.get("COMET_CC_HOME") or Path.home() / ".comet-cc")
    p.mkdir(parents=True, exist_ok=True)
    return p


def store_path() -> Path:
    return home() / "store.sqlite"


def l1_path(session_id: str) -> Path:
    d = home() / "l1"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{session_id}.jsonl"


def log_path() -> Path:
    d = home() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d / "hook.log"


def daemon_socket() -> Path:
    return home() / "daemon.sock"


def daemon_pid_file() -> Path:
    return home() / "daemon.pid"


def daemon_log() -> Path:
    d = home() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d / "daemon.log"


# Sensor gating — maps to CoMeT's compacting.{max_l1_buffer, min_l1_buffer, load_threshold}
MAX_L1_BUFFER = int(os.environ.get("COMET_CC_MAX_L1", "20"))
MIN_L1_BUFFER = int(os.environ.get("COMET_CC_MIN_L1", "3"))
LOAD_THRESHOLD = int(os.environ.get("COMET_CC_LOAD_THRESHOLD", "4"))

# Retrieval
MAX_CONTEXT_NODES = int(os.environ.get("COMET_CC_MAX_CONTEXT_NODES", "8"))
MIN_SIMILARITY = float(os.environ.get("COMET_CC_MIN_SIM", "0.30"))

# Tail of L1 buffer passed to the sensor as "prior context"
SENSOR_BUFFER_TAIL = 5
