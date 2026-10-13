"""Rebuild the runs list from the run folders under the UI output root (`<root>/<run_id>/`)."""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from src.ui import statuses
from src.ui.events import DONE, ERROR, EVENTS_FILE, RUN_FILE, RunHub

logger = logging.getLogger("ui.run_store")

STOPPED_NOTE = "The server stopped before this run finished."


def relative_paths(run_dir: Path, paths: dict[str, str]) -> dict[str, str]:
    """Output paths relative to the run folder, so a saved run still opens from another cwd."""
    out = {}
    for key, value in paths.items():
        try:
            out[key] = Path(value).resolve().relative_to(run_dir.resolve()).as_posix()
        except (ValueError, OSError):
            out[key] = str(value)
    return out


def resolve_path(run_dir: Path, value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else run_dir / p


def summary_from_trace(trace: dict[str, Any]) -> dict[str, Any]:
    """Dashboard counts for a run saved before run.json existed."""
    findings = trace.get("findings") or []
    blocking = [f for f in findings if f.get("status") == "failed" and f.get("severity") == "error"]
    warnings = [f for f in findings if f.get("status") == "warning"
                or (f.get("status") == "failed" and f.get("severity") != "error")]
    return {
        "rules_passed": sum(1 for f in findings if f.get("status") == "passed"),
        "blocking": len(blocking),
        "warnings": len(warnings),
        "not_checked": sum(1 for f in findings if f.get("status") == "not_evaluated"),
        "fields_extracted": sum(1 for f in trace.get("fields") or [] if f.get("value") not in (None, "")),
        "turns": (trace.get("run") or {}).get("turns"),
        "duration_s": None,
    }


def load_all(output_root: str | Path) -> list[RunHub]:
    root = Path(output_root)
    if not root.is_dir():
        return []
    hubs = []
    for run_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        try:
            hub = _load_one(run_dir)
        except Exception as exc:  # one unreadable folder must not hide the others
            logger.warning("Skipping run folder %s: %s", run_dir, exc)
            continue
        if hub is not None:
            hubs.append(hub)
    return hubs


def _load_one(run_dir: Path) -> RunHub | None:
    if (run_dir / RUN_FILE).is_file():
        return _from_record(run_dir, json.loads((run_dir / RUN_FILE).read_text(encoding="utf-8")))
    pdfs = sorted(run_dir.glob("*.pdf"))
    if not pdfs:
        return None  # not a run folder
    return _backfill(run_dir, pdfs[0])


def _from_record(run_dir: Path, rec: dict[str, Any]) -> RunHub:
    hub = RunHub(rec["run_id"], rec["filename"], pdf_path=str(run_dir / rec["filename"]), run_dir=run_dir)
    hub.created_at = rec.get("created_at") or hub.created_at
    hub.content_sha256 = rec.get("content_sha256") or ""
    hub.status = rec.get("status") or statuses.ERROR
    hub.verdict = rec.get("verdict") or ""
    hub.history = list(rec.get("history") or [])
    hub.summary = dict(rec.get("summary") or {})
    hub.invoice_type = rec.get("invoice_type") or ""
    hub.paths = dict(rec.get("paths") or {})
    hub.reevaluated_from = rec.get("reevaluated_from") or ""
    hub.reevaluated_to = rec.get("reevaluated_to") or ""
    hub.field_edits = dict(rec.get("field_edits") or {})

    events_path = run_dir / EVENTS_FILE
    if events_path.is_file():
        events = []
        for line in events_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    break  # a line cut off by a crash: keep what came before
        hub.load_events(events)

    if statuses.is_active(hub.status):
        # It was queued or running when the server stopped; nothing will finish it now.
        # The run_error event also ends the browser's event stream for this run.
        hub.publish("run_error", message=STOPPED_NOTE)
        hub.set_status(statuses.ERROR, by="system", note=STOPPED_NOTE)
        hub.set_state(ERROR)
    else:
        hub.set_state(ERROR if hub.status == statuses.ERROR else DONE)
    return hub


def _backfill(run_dir: Path, pdf: Path) -> RunHub:
    """A run saved before the dashboard existed: rebuild its record from the trace, then save it."""
    hub = RunHub(run_dir.name, pdf.name, pdf_path=str(pdf), run_dir=run_dir)
    hub.created_at = pdf.stat().st_mtime
    hub.content_sha256 = hashlib.sha256(pdf.read_bytes()).hexdigest()

    traces = sorted((run_dir / "result").glob("trace_*.json"))
    if not traces:
        hub._set_status(statuses.ERROR, "system", "No results were saved for this run.", at=hub.created_at)
        hub.set_state(ERROR)
        hub.save()
        return hub

    trace = json.loads(traces[0].read_text(encoding="utf-8"))
    doc, run = trace.get("document") or {}, trace.get("run") or {}
    hub.verdict = run.get("status") or ""
    hub.invoice_type = doc.get("invoice_type_name") or doc.get("invoice_type_id") or ""
    hub.summary = summary_from_trace(trace)
    paths = {"trace_json": str(traces[0])}
    flagged = sorted((run_dir / "result").glob("flagged_*.pdf"))
    if flagged:
        paths["flagged_pdf"] = str(flagged[0])
    hub.paths = relative_paths(run_dir, paths)
    status = statuses.status_from_verdict(hub.verdict)
    hub._set_status(status, "agent", f"Agent decision: {statuses.verdict_label(hub.verdict)}",
                    at=traces[0].stat().st_mtime)
    hub.set_state(ERROR if status == statuses.ERROR else DONE)
    hub.save()
    return hub
