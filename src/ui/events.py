"""Per-run event log that the web presenter writes to and the SSE endpoint reads from."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from src.ui import statuses

logger = logging.getLogger("ui.events")

# Run lifecycle states shown to the browser.
QUEUED = "queued"
RUNNING = "running"
DONE = "done"
ERROR = "error"

RUN_FILE = "run.json"
EVENTS_FILE = "events.jsonl"


class RunHub:
    """
    Append-only, thread-safe event list for one run.

    The agent thread publishes; the SSE endpoint reads from an index (`events_after`), so a
    browser that connects late or refreshes mid-run replays the full history.

    With a `run_dir`, every event is appended to `events.jsonl` and the run's record
    (workflow status, history, summary) to `run.json`, so the dashboard survives a restart.
    `state` is the stream lifecycle (it ends the SSE stream); `status` is the workflow status.
    """

    def __init__(self, run_id: str, filename: str, pdf_path: str = "", run_dir: str | Path | None = None) -> None:
        self.run_id = run_id
        self.filename = filename
        self.pdf_path = pdf_path
        self.run_dir = Path(run_dir) if run_dir is not None else None
        self.state = QUEUED
        self.created_at = time.time()
        self.status = statuses.NEW
        self.verdict = ""
        self.history: list[dict[str, Any]] = []
        self.summary: dict[str, Any] = {}
        self.content_sha256 = ""
        self.invoice_type = ""
        self.paths: dict[str, str] = {}
        self.reevaluated_from = ""
        self.reevaluated_to = ""
        # Reviewer corrections of extracted values: field_name -> {value, at, by}.
        self.field_edits: dict[str, dict[str, Any]] = {}
        # Set by the runner when the run produced a flagged PDF (results view in the browser).
        self.result: dict[str, Any] | None = None
        self.flagged_pdf: str = ""
        self.page_images: dict[int, str] = {}
        self._events: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._save_lock = threading.Lock()

    def publish(self, event_type: str, **data: Any) -> dict[str, Any]:
        with self._lock:
            event = {"seq": len(self._events) + 1, "type": event_type, "ts": time.time(), **data}
            self._events.append(event)
            if self.run_dir is not None:
                try:
                    with open(self.run_dir / EVENTS_FILE, "a", encoding="utf-8") as f:
                        f.write(json.dumps(event, default=str) + "\n")
                except OSError as exc:  # a lost log line must not stop the run
                    logger.warning("Could not save event for run %s: %s", self.run_id, exc)
            return event

    def load_events(self, events: list[dict[str, Any]]) -> None:
        """Restore a saved run's history without writing it again."""
        with self._lock:
            self._events = list(events)

    def events_after(self, seq: int) -> list[dict[str, Any]]:
        with self._lock:
            return self._events[seq:]

    def set_state(self, state: str) -> None:
        with self._lock:
            self.state = state

    @property
    def finished(self) -> bool:
        return self.state in (DONE, ERROR)

    # --- workflow status -------------------------------------------------------------

    def set_status(self, status: str, *, by: str, note: str = "") -> None:
        with self._lock:
            self._set_status(status, by, note)
        self.save()

    def transition(self, allowed_from: list[str], status: str, *, by: str, note: str = "") -> bool:
        """Move to `status` only from one of `allowed_from`, checked and changed atomically."""
        with self._lock:
            if self.status not in allowed_from:
                return False
            self._set_status(status, by, note)
        self.save()
        return True

    def _set_status(self, status: str, by: str, note: str, at: float | None = None) -> None:
        self.status = status
        self.history.append({"status": status, "at": at or time.time(), "by": by, "note": note})

    def set_field_edit(self, field_name: str, value: str | None, *, by: str) -> None:
        """Save a reviewer's value for a field; `None` drops the edit (back to the AI's value)."""
        with self._lock:
            if value is None:
                self.field_edits.pop(field_name, None)
            else:
                self.field_edits[field_name] = {"value": value, "at": time.time(), "by": by}
        self.save()

    def update(self, **fields: Any) -> None:
        """Set record fields (verdict, summary, paths, …) and save them."""
        with self._lock:
            for key, value in fields.items():
                setattr(self, key, value)
        self.save()

    # --- persistence -----------------------------------------------------------------

    def record(self) -> dict[str, Any]:
        """What `run.json` holds."""
        with self._lock:
            return {
                "run_id": self.run_id,
                "filename": self.filename,
                "created_at": self.created_at,
                "content_sha256": self.content_sha256,
                "status": self.status,
                "verdict": self.verdict,
                "history": list(self.history),
                "summary": dict(self.summary),
                "invoice_type": self.invoice_type,
                "paths": dict(self.paths),
                "reevaluated_from": self.reevaluated_from,
                "reevaluated_to": self.reevaluated_to,
                "field_edits": {k: dict(v) for k, v in self.field_edits.items()},
            }

    def save(self) -> None:
        if self.run_dir is None:
            return
        record = self.record()
        # Worker and API threads both save; the lock keeps the tmp file to one writer.
        with self._save_lock:
            tmp = self.run_dir / f"{RUN_FILE}.tmp"
            try:
                tmp.write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
                os.replace(tmp, self.run_dir / RUN_FILE)
            except OSError as exc:
                logger.warning("Could not save run %s: %s", self.run_id, exc)

    def snapshot(self) -> dict[str, Any]:
        record = self.record()
        record.pop("paths")
        with self._lock:
            return {
                **record,
                "pdf_path": self.pdf_path,
                "state": self.state,
                "event_count": len(self._events),
                "has_result": bool(self.paths.get("trace_json") and self.paths.get("flagged_pdf")),
            }
