"""
End-of-run hook: build `trace.json` and the flagged PDF next to the run's other outputs.

Called from `InvoiceAgent.run` once the agent has finished (any status except the early
"unknown invoice type" exit, which has no pages), so the CLI and the worker both get the
files without changing their call sites. Tracing never raises: a failure is
logged and the run's own results stay intact.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from src.trace.export import build_trace
from src.trace.ocr_cache import page_ocr

logger = logging.getLogger(__name__)


def write_trace_outputs(
    state: Any,
    store: Any,
    config: dict,
    surya_models: Any = None,
    silent: bool = False,
) -> dict[str, str]:
    cfg = config.get("traceability", {}) or {}
    if not cfg.get("enabled", False):
        return {}
    out_dir = Path(state.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(state.pdf_path).stem
    paths: dict[str, str] = {}
    max_ocr = int(cfg.get("export_ocr_max_pages", 12) or 0)

    def ocr_missing(pages: list[int]) -> None:
        if surya_models is None or max_ocr <= 0:
            return
        for p in pages[:max_ocr]:
            if 1 <= p <= len(state.page_image_paths or []):
                page_ocr(state, p, state.page_image_paths[p - 1], surya_models, silent=silent)

    try:
        trace = build_trace(state, store, config, ocr_missing_pages=ocr_missing)
        trace_path = out_dir / f"trace_{stem}.json"
        trace_path.write_text(
            json.dumps(trace, ensure_ascii=False, indent=1, default=str, allow_nan=False), encoding="utf-8"
        )
        paths["trace_json"] = str(trace_path)
    except Exception as e:
        logger.error("Trace export failed (run results unaffected): %s", e, exc_info=True)
        return paths

    if not state.page_count or not Path(state.pdf_path).exists():
        return paths
    try:
        from src.trace.pdf_writer import render_flagged_pdf

        pdf_path = out_dir / f"flagged_{stem}.pdf"
        render_flagged_pdf(trace, state.pdf_path, str(pdf_path), getattr(state, "page_rotation", None))
        paths["flagged_pdf"] = str(pdf_path)
        s = trace["summary"]
        logger.info(
            "Flagged PDF: %s (%d/%d fields located)", pdf_path, s["fields_located"], s["fields_with_value"]
        )
    except Exception as e:
        logger.error("Flagged PDF rendering failed (trace.json kept): %s", e, exc_info=True)
    return paths
