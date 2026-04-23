"""Per-session state held by the trim orchestrator.

Memory-only; resets on daemon restart. Losing state just means the first
few turns after restart passthrough until the sensor trips again — no
correctness risk since the store still has all previously saved nodes.
"""
from __future__ import annotations

import threading
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

    # True while the compact LLM call is actually running. Worker sets it;
    # request path reads it to avoid spawning duplicate compacts.
    compact_in_flight: bool = False

    # True once a sensor_check job for this session is in the queue or
    # being worked on. Prevents a burst of CC requests from queueing N
    # identical checks. Worker clears it on pickup.
    sensor_queued: bool = False


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

    def mark_sensor_queued(self, session_id: str) -> None:
        with self._lock:
            s = self._states.get(session_id)
            if s:
                s.sensor_queued = True

    def mark_sensor_pickup(self, session_id: str) -> None:
        """Worker calls this when it dequeues a sensor_check, reopening the
        queue slot for the next request to enqueue as needed."""
        with self._lock:
            s = self._states.get(session_id)
            if s:
                s.sensor_queued = False

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
            if summary_user and summary_asst:
                s.absorbed_fps.update(absorbed_fps)
                s.summary_user = summary_user
                s.summary_asst = summary_asst

