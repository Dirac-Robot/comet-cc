"""Per-session state held by the trim orchestrator.

Memory-only; resets on daemon restart. Losing state just means the first
few turns after restart passthrough until the sensor trips again — no
correctness risk since the store still has all previously saved nodes.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class SessionState:
    session_id: str

    # Fingerprints of messages that are covered by the current summary pair.
    # Whenever we see these again in an incoming request, we drop them and
    # inject the summary pair in their place.
    absorbed_fps: set[str] = field(default_factory=set)

    # The summary pair: injected as (user, assistant) before the unabsorbed
    # tail. None if no compaction has happened yet.
    summary_user: str | None = None
    summary_asst: str | None = None

    # True while a sensor/compact job is in-flight for this session. Prevents
    # re-queuing on every request when the worker is already busy.
    compact_in_flight: bool = False

    # Last time we kicked a sensor check for this session. Throttles how
    # often we poll the sensor (monotonic clock seconds).
    last_sensor_check: float = 0.0


class SessionRegistry:
    """Thread-safe map session_id -> SessionState."""

    def __init__(self) -> None:
        self._states: dict[str, SessionState] = {}
        self._lock = threading.Lock()

    def get_or_create(self, session_id: str) -> SessionState:
        with self._lock:
            s = self._states.get(session_id)
            if s is None:
                s = SessionState(session_id=session_id)
                self._states[session_id] = s
            return s

    def get(self, session_id: str) -> SessionState | None:
        with self._lock:
            return self._states.get(session_id)

    def snapshot(self) -> list[SessionState]:
        with self._lock:
            return list(self._states.values())

    def mark_compact_start(self, session_id: str) -> None:
        with self._lock:
            s = self._states.get(session_id)
            if s:
                s.compact_in_flight = True

    def mark_compact_done(
        self, session_id: str,
        absorbed_fps: set[str],
        summary_user: str | None,
        summary_asst: str | None,
    ) -> None:
        with self._lock:
            s = self._states.get(session_id)
            if not s:
                return
            s.compact_in_flight = False
            # Reset the throttle clock: the next incoming request should be
            # free to queue a fresh sensor_check without waiting another
            # SENSOR_THROTTLE_SEC window. Critical for compact granularity
            # when a long compact ran while the user kept typing.
            s.last_sensor_check = 0.0
            if summary_user and summary_asst:
                s.absorbed_fps.update(absorbed_fps)
                s.summary_user = summary_user
                s.summary_asst = summary_asst

    def touch_sensor(self, session_id: str) -> None:
        with self._lock:
            s = self._states.get(session_id)
            if s:
                s.last_sensor_check = time.monotonic()
