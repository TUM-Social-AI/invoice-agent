"""Run logging and small config slices shared by loop and pipeline."""

from __future__ import annotations

import json
import logging
import time as _time
from pathlib import Path
from typing import Any

from src.agent.agent_settings import clip_for_log
from src.llm.config_resolve import llm_timeouts

logger = logging.getLogger(__name__)


def timeout_cfg(config: dict) -> dict:
    return llm_timeouts(config)


def open_run_log(output_dir: str) -> tuple[str, Any]:
    logs_dir = Path(output_dir) / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    timestamp = _time.strftime("%Y%m%d_%H%M%S")
    log_path = logs_dir / f"agent_log_{timestamp}.jsonl"
    handle = open(log_path, "a", encoding="utf-8")
    return str(log_path), handle


def append_log_entry(handle: Any, entry: dict) -> None:
    try:
        handle.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        handle.flush()
    except Exception:
        pass


def log_tool_result(tool_name: str, result: Any, log_line_max_chars: int) -> None:
    """Log a human-readable summary of a tool result."""
    clip = lambda s: clip_for_log(s, log_line_max_chars)

    if not isinstance(result, dict):
        logger.info(f"  result:    {clip(str(result))}")
        return

    success = result.get("success", True)
    status = "OK" if success else "FAIL"

    if tool_name == "inspect_file":
        logger.info(
            f"  result [{status}]: pages={result.get('page_count')}, "
            f"size={result.get('size_mb')}MB, format={result.get('format')}"
        )
    elif tool_name == "convert_pdf_to_images":
        paths = result.get("page_paths") or []
        out = str(Path(paths[0]).parent) if paths else "?"
        logger.info(f"  result [{status}]: rendered {result.get('page_count')} pages → {out}")
    elif tool_name == "compress_pages":
        paths = result.get("page_paths") or []
        out = str(Path(paths[0]).parent) if paths else "?"
        logger.info(f"  result [{status}]: {result.get('page_count')} pages → {out}")
    elif tool_name == "classify_document_type":
        logger.info(
            f"  result [{status}]: type={result.get('invoice_type_id')}, "
            f"confidence={result.get('confidence')}"
        )
    elif tool_name == "crop_region":
        logger.info(f"  result [{status}]: cropped → {result.get('crop_path')}")
    elif tool_name == "extract_fields_vision":
        extracted = result.get("extracted", {})
        merge = result.get("merge_result", {})
        payload_non_null = sum(
            1 for k, v in extracted.items()
            if not k.endswith("_confidence") and v is not None
        )
        upd = merge.get("updated") if merge else []
        merge_updates = len(upd) if isinstance(upd, list) else 0
        low_conf = [k for k, v in extracted.items() if isinstance(v, dict) and v.get("confidence", 1) < 0.6]
        logger.info(
            f"  result [{status}]: payload non-null={payload_non_null}, "
            f"state merge updates={merge_updates} (hybrid OCR may merge without non-null vision payload)"
            + (f", low-confidence: {low_conf}" if low_conf else "")
        )
        if merge:
            _kept_raw = merge.get("already_have_better", [])
            _null_raw = merge.get("null_fields", [])
            kept = _kept_raw if isinstance(_kept_raw, int) else len(_kept_raw)
            nulls = _null_raw if isinstance(_null_raw, int) else len(_null_raw)
            kept_note = f" (already have better — do NOT retry these)" if kept else ""
            null_note = f", null_fields={nulls} (model returned null — try crop_region or move on)" if nulls else ""
            logger.info(f"  merge:     updated={merge.get('updated')}, kept_existing={kept}{kept_note}{null_note}")
    elif tool_name == "check_compliance":
        errors = result.get("failed_errors", [])
        warnings = result.get("failed_warnings", [])
        skipped = result.get("skipped_checks", [])
        visual_pending = result.get("visual_checks_pending", [])
        logger.info(
            f"  result [{status}]: passed={result.get('passed')}, "
            f"errors={len(errors)}, warnings={len(warnings)}, "
            f"skipped={len(skipped)}, visual_pending={len(visual_pending)}"
        )
        for e in errors:
            logger.info(f"    ERROR:   {clip(str(e))}")
        for w in warnings:
            logger.info(f"    WARN:    {clip(str(w))}")
        for s in skipped:
            logger.info(
                f"    SKIPPED: {s['rule_id']} [{s['severity']}] — {clip(str(s.get('reason', '')))}"
            )
        if visual_pending:
            logger.info(f"    VISUAL PENDING: {visual_pending}")
    elif tool_name == "check_compliance_visual":
        errors = result.get("failed_errors", [])
        warnings = result.get("failed_warnings", [])
        logger.info(
            f"  result [{status}]: checked={result.get('visual_rules_checked')}, "
            f"passed={result.get('passed')}, errors={len(errors)}, warnings={len(warnings)}"
        )
        if not success and result.get("error"):
            logger.info(f"    ERROR: {clip(str(result.get('error')))}")
        for e in errors:
            logger.info(f"    ERROR: {clip(str(e))}")
        for w in warnings:
            logger.info(f"    WARN:  {clip(str(w))}")
        bf = result.get("backfilled_fields") or []
        if bf:
            logger.info(f"    backfilled_fields: {bf}")
    elif tool_name == "note":
        logger.info(f"  noted: {clip(result.get('noted', ''))}")
    elif tool_name == "inventory_pages":
        inventory = result.get("inventory", [])
        logger.info(f"  result [{status}]: scanned {len(inventory)} pages")
        for entry in inventory:
            cat = entry.get("category", "?")
            desc = clip(str(entry.get("description", "")))
            logger.info(f"    p{entry['page']} [{cat}]: {desc}")
    elif tool_name == "install_package":
        pkg = result.get("package", "?")
        logger.info(f"  result [{status}]: pip install {pkg}")
        if not result.get("success"):
            logger.info(f"    stderr: {clip(result.get('stderr', ''))}")
    elif tool_name == "write_learning":
        logger.info(f"  result [{status}]: learning written")
    elif tool_name == "finish":
        ex = result.get("status_explanation")
        logger.info(
            f"  result: status={result.get('status')} | "
            f"errors={result.get('error_failures', [])} | "
            f"warnings={result.get('warning_failures', [])}"
            + (f" | {ex}" if ex else "")
        )
    else:
        if not success:
            logger.info(f"  result [FAIL]: {clip(result.get('error', str(result)))}")
        else:
            logger.info(f"  result [OK]")
