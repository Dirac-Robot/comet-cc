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


# ---------- Proxy architecture (new) ----------

def cert_dir() -> Path:
    d = home() / "certs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def proxy_log() -> Path:
    d = home() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d / "proxy.log"


PROXY_HOST = os.environ.get("COMET_CC_PROXY_HOST", "127.0.0.1")
PROXY_PORT = int(os.environ.get("COMET_CC_PROXY_PORT", "8443"))
UPSTREAM_URL = os.environ.get("COMET_CC_UPSTREAM", "https://api.anthropic.com")


# Retrieval is session-scoped by default — CoMeT-CC doesn't support session
# handoff, so leaking another session's memory would be surprising. Flip
# `COMET_CC_CROSS_SESSION=1` to get global retrieval across all sessions
# (useful if you care about passive/rule nodes transcending /compact,
# /clear, or --resume boundaries).
def _env_flag(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


CROSS_SESSION_RETRIEVAL = _env_flag("COMET_CC_CROSS_SESSION", default=False)


# ---------- Graph linking (similarity-based cross-link + hop expansion) ----------

# Min cosine similarity between two nodes to auto-add a bidirectional `links`
# edge when a new node is compacted. Matches full CoMeT (1 - 0.45 distance).
CROSS_LINK_SIM_THRESHOLD = float(
    os.environ.get("COMET_CC_CROSS_LINK_SIM", "0.55")
)
CROSS_LINK_TOP_K = int(os.environ.get("COMET_CC_CROSS_LINK_TOP_K", "10"))

# Retrieval graph expansion: after the initial cosine match, walk one hop
# through each top-result's links and surface neighbors with a relevance
# decay. Set the decay to 0 to disable expansion.
HOP1_DECAY = float(os.environ.get("COMET_CC_HOP1_DECAY", "0.5"))
