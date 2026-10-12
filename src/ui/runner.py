"""Owns the single InvoiceAgent and processes uploaded PDFs one at a time."""

from __future__ import annotations

import logging
import queue
import re
import threading
import uuid
from pathlib import Path
from typing import Any, Callable

from src.ui.events import DONE, ERROR, RUNNING, RunHub
from src.ui.web_presenter import WebPresenter

logger = logging.getLogger("ui.runner")

MAX_UPLOAD_BYTES = 50 * 1024 * 1024


class UploadError(ValueError):
    """Raised for uploads the UI refuses (not a PDF, empty, too large)."""


def _safe_filename(name: str) -> str:
    stem = Path(name or "invoice.pdf").stem
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._") or "invoice"
    return f"{stem[:80]}.pdf"


class RunManager:
    """
    The agent is expensive to build (OCR models) and its presenter is bound at construction,
    so one agent serves every run through a single worker thread; extra uploads wait in line.

    `agent` needs `.run(pdf_path, output_dir, source_provenance=..., run_identity=...)`.
    """

    def __init__(
        self,
        agent: Any,
        presenter: WebPresenter,
        output_root: str | Path = "output/ui",
        *,
        materialize: Callable[[str], Any] | None = None,
        write_results: Callable[[Any, str], dict[str, str]] | None = None,
        page_render_scale: float = 2.0,
    ) -> None:
        self.agent = agent
        self.presenter = presenter
        self.output_root = Path(output_root)
        self._materialize = materialize
        self._write_results = write_results
        self.page_render_scale = page_render_scale
        self._runs: dict[str, RunHub] = {}
        self._queue: queue.Queue[str] = queue.Queue()
        self._worker = threading.Thread(target=self._work, name="ui-run-worker", daemon=True)
        self._worker.start()

    # --- public API ------------------------------------------------------------------

    def submit(self, filename: str, data: bytes) -> RunHub:
        if not data:
            raise UploadError("The uploaded file is empty.")
        if len(data) > MAX_UPLOAD_BYTES:
            raise UploadError(f"File too large (limit {MAX_UPLOAD_BYTES // (1024 * 1024)} MB).")
        if not data.lstrip().startswith(b"%PDF"):
            raise UploadError("That doesn't look like a PDF file.")

        run_id = uuid.uuid4().hex[:12]
        run_dir = self.output_root / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        pdf_path = run_dir / _safe_filename(filename)
        pdf_path.write_bytes(data)

        hub = RunHub(run_id, pdf_path.name, pdf_path=str(pdf_path))
        self._runs[run_id] = hub
        hub.publish("run_queued", filename=hub.filename, ahead=self._queue.qsize() + self._busy())
        self._queue.put(run_id)
        return hub

    def get(self, run_id: str) -> RunHub | None:
        return self._runs.get(run_id)

    def list_runs(self) -> list[dict[str, Any]]:
        runs = sorted(self._runs.values(), key=lambda h: h.created_at, reverse=True)
        return [h.snapshot() for h in runs]

    # --- worker ----------------------------------------------------------------------

    def _busy(self) -> int:
        return sum(1 for h in self._runs.values() if h.state == RUNNING)

    def _work(self) -> None:
        while True:
            run_id = self._queue.get()
            hub = self._runs[run_id]
            try:
                self._process(hub)
            except Exception as exc:  # the worker must outlive any single bad run
                logger.error("Run %s failed: %s", run_id, exc, exc_info=True)
                hub.publish("run_error", message=str(exc) or exc.__class__.__name__)
                hub.set_state(ERROR)
            finally:
                self.presenter.detach()
                self._queue.task_done()

    def _process(self, hub: RunHub) -> None:
        materialize = self._materialize
        write_results = self._write_results
        if materialize is None:
            from src.sources.local import materialize_local_input

            materialize = materialize_local_input
        if write_results is None:
            from src.output.writer import write_results as default_write

            write_results = default_write

        hub.set_state(RUNNING)
        self.presenter.attach(hub)

        doc = materialize(hub.pdf_path)[0]
        out_dir = str(self.output_root / hub.run_id / "result")
        state = self.agent.run(
            pdf_path=doc.local_pdf_path,
            output_dir=out_dir,
            source_provenance=doc.provenance,
            run_identity=doc.run_identity,
        )
        paths = write_results(state, out_dir)
        # Before run_complete, so the browser can fetch the results view as soon as it hears of it.
        self._attach_result(hub, paths)
        self.presenter.run_complete(state, paths=paths)
        hub.set_state(DONE)

    def _attach_result(self, hub: RunHub, paths: dict[str, str]) -> None:
        from src.ui.result_view import load_result

        try:
            loaded = load_result(paths, hub.run_id, self.output_root / hub.run_id / "pages", self.page_render_scale)
        except Exception as exc:  # no results view is not a failed run
            logger.error("Results view for run %s failed: %s", hub.run_id, exc, exc_info=True)
            return
        if loaded is None:
            return
        hub.result, hub.page_images = loaded
        hub.flagged_pdf = paths["flagged_pdf"]
