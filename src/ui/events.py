"""Per-run event log that the web presenter writes to and the SSE endpoint reads from."""

from __future__ import annotations

import threading
import time
from typing import Any

# Run lifecycle states shown to the browser.
QUEUED = "queued"
RUNNING = "running"
DONE = "done"
ERROR = "error"


class RunHub:
    """
    Append-only, thread-safe event list for one run.

    The agent thread publishes; the SSE endpoint reads from an index (`events_after`), so a
    browser that connects late or refreshes mid-run replays the full history.
    """

    def __init__(self, run_id: str, filename: str, pdf_path: str = "") -> None:
        self.run_id = run_id
        self.filename = filename
        self.pdf_path = pdf_path
        self.state = QUEUED
        self.created_at = time.time()
        self._events: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def publish(self, event_type: str, **data: Any) -> dict[str, Any]:
        with self._lock:
            event = {"seq": len(self._events) + 1, "type": event_type, "ts": time.time(), **data}
            self._events.append(event)
            return event

    def events_after(self, seq: int) -> list[dict[str, Any]]:
        with self._lock:
            return self._events[seq:]

    def set_state(self, state: str) -> None:
        with self._lock:
            self.state = state

    @property
    def finished(self) -> bool:
        return self.state in (DONE, ERROR)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "run_id": self.run_id,
                "filename": self.filename,
                "pdf_path": self.pdf_path,
                "state": self.state,
                "event_count": len(self._events),
                "created_at": self.created_at,
            }
