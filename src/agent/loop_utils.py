"""Run logging and small config slices shared by loop and pipeline."""

from __future__ import annotations

import json
import logging
import time as _time
from dataclasses import dataclass, field
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


@dataclass
class ToolResultSummary:
    success: bool
    primary: str
    details: list[str] = field(default_factory=list)


def summarize_tool_result(
    tool_name: str, result: Any, log_line_max_chars: int
) -> ToolResultSummary:
    """Build a human-readable summary of a tool result (shared by logs and presenter)."""
    clip = lambda s: clip_for_log(s, log_line_max_chars)

    if not isinstance(result, dict):
        return ToolResultSummary(success=True, primary=clip(str(result)))

    success = result.get("success", True)
    status = "OK" if success else "FAIL"

    if tool_name == "inspect_file":
        return ToolResultSummary(
            success=success,
            primary=(
                f"{result.get('page_count')} pages · "
                f"{result.get('format', '?')} · {result.get('size_mb')} MB"
            ),
        )
    if tool_name == "convert_pdf_to_images":
        return ToolResultSummary(
            success=success,
            primary=f"{result.get('page_count')} pages rendered",
        )
    if tool_name == "compress_pages":
        return ToolResultSummary(
            success=success,
            primary=f"{result.get('page_count')} thumbnail pages",
        )
    if tool_name == "classify_document_type":
        conf = result.get("confidence")
        conf_s = f"{conf:.0%}" if isinstance(conf, (int, float)) else str(conf)
        return ToolResultSummary(
            success=success,
            primary=f"{result.get('invoice_type_id')} (confidence {conf_s})",
        )
    if tool_name == "crop_region":
        return ToolResultSummary(
            success=success,
            primary=f"cropped → {result.get('crop_path')}",
        )
    if tool_name == "extract_fields_vision":
        extracted = result.get("extracted", {})
        merge = result.get("merge_result", {})
        upd = merge.get("updated") if merge else []
        updated = upd if isinstance(upd, list) else []
        details: list[str] = []
        if updated:
            details.append(f"Updated: {', '.join(str(u) for u in updated)}")
        low_conf = [
            k for k, v in extracted.items()
            if isinstance(v, dict) and v.get("confidence", 1) < 0.6
        ]
        if low_conf:
            details.append(f"Low confidence: {', '.join(low_conf)}")
        if merge:
            _kept_raw = merge.get("already_have_better", [])
            _null_raw = merge.get("null_fields", [])
            kept = _kept_raw if isinstance(_kept_raw, int) else len(_kept_raw)
            nulls = _null_raw if isinstance(_null_raw, int) else len(_null_raw)
            if kept:
                details.append(f"Kept existing (better confidence): {kept} field(s)")
            if nulls:
                details.append(f"Null fields: {nulls} (model returned no value)")
        payload_non_null = sum(
            1 for k, v in extracted.items()
            if not k.endswith("_confidence") and v is not None
        )
        merge_updates = len(updated)
        primary = (
            f"{merge_updates} field(s) merged"
            if merge_updates
            else f"{payload_non_null} non-null value(s) in payload"
        )
        return ToolResultSummary(success=success, primary=primary, details=details)
    if tool_name == "check_compliance":
        errors = result.get("failed_errors", [])
        warnings = result.get("failed_warnings", [])
        skipped = result.get("skipped_checks", [])
        visual_pending = result.get("visual_checks_pending", [])
        primary = (
            f"{result.get('passed')} passed · {len(errors)} blocking error(s) · "
            f"{len(warnings)} warning(s)"
        )
        details = []
        for e in errors:
            details.append(f"ERROR: {clip(str(e))}")
        for w in warnings:
            details.append(f"WARN: {clip(str(w))}")
        for s in skipped:
            details.append(
                f"SKIPPED: {s['rule_id']} [{s['severity']}] — {clip(str(s.get('reason', '')))}"
            )
        if visual_pending:
            details.append(f"Visual checks pending: {visual_pending}")
        return ToolResultSummary(success=success, primary=primary, details=details)
    if tool_name == "check_compliance_visual":
        errors = result.get("failed_errors", [])
        warnings = result.get("failed_warnings", [])
        checked = result.get("visual_rules_checked", 0)
        passed = result.get("passed", 0)
        primary = f"{checked} rule(s) checked · {passed} passed"
        details = []
        if not success and result.get("error"):
            details.append(f"ERROR: {clip(str(result.get('error')))}")
        for e in errors:
            details.append(f"ERROR: {clip(str(e))}")
        for w in warnings:
            details.append(f"WARN: {clip(str(w))}")
        bf = result.get("backfilled_fields") or []
        if bf:
            details.append(f"Backfilled fields: {', '.join(str(x) for x in bf)}")
        return ToolResultSummary(success=success, primary=primary, details=details)
    if tool_name == "note":
        return ToolResultSummary(success=success, primary=clip(result.get("noted", "")))
    if tool_name == "inventory_pages":
        inventory = result.get("inventory", [])
        details = [
            f"p{entry['page']}  {entry.get('category', '?')}   {clip(str(entry.get('description', '')))}"
            for entry in inventory
        ]
        return ToolResultSummary(
            success=success,
            primary=f"{len(inventory)} page(s) mapped",
            details=details,
        )
    if tool_name == "install_package":
        details = []
        if not result.get("success"):
            details.append(f"stderr: {clip(result.get('stderr', ''))}")
        return ToolResultSummary(
            success=success,
            primary=f"pip install {result.get('package', '?')}",
            details=details,
        )
    if tool_name == "write_learning":
        return ToolResultSummary(success=success, primary="Learning written")
    if tool_name == "finish":
        ex = result.get("status_explanation")
        primary = (
            f"status={result.get('status')} · "
            f"errors={result.get('error_failures', [])} · "
            f"warnings={result.get('warning_failures', [])}"
        )
        if ex:
            primary += f" · {clip(str(ex))}"
        return ToolResultSummary(success=success, primary=primary)

    if not success:
        return ToolResultSummary(
            success=False,
            primary=clip(result.get("error", str(result))),
        )
    return ToolResultSummary(success=success, primary="completed")


def log_tool_result(tool_name: str, result: Any, log_line_max_chars: int) -> None:
    """Log a human-readable summary of a tool result."""
    summary = summarize_tool_result(tool_name, result, log_line_max_chars)
    status = "OK" if summary.success else "FAIL"

    if tool_name in (
        "inspect_file", "convert_pdf_to_images", "compress_pages",
        "classify_document_type", "crop_region", "extract_fields_vision",
        "check_compliance", "check_compliance_visual", "inventory_pages",
        "install_package", "write_learning",
    ):
        logger.info(f"  result [{status}]: {summary.primary}")
        for line in summary.details:
            if line.startswith("ERROR:"):
                logger.info(f"    ERROR:   {line[6:].strip()}")
            elif line.startswith("WARN:"):
                logger.info(f"    WARN:    {line[5:].strip()}")
            elif line.startswith("SKIPPED:"):
                logger.info(f"    SKIPPED: {line[8:].strip()}")
            elif line.startswith("Visual checks pending:"):
                logger.info(f"    VISUAL PENDING: {line.split(':', 1)[1].strip()}")
            elif line.startswith("Updated:") or line.startswith("Low confidence:"):
                logger.info(f"  merge:     {line}")
            elif line.startswith("Kept existing") or line.startswith("Null fields:"):
                logger.info(f"  merge:     {line}")
            elif line.startswith("p") and "  " in line:
                logger.info(f"    {line}")
            elif line.startswith("stderr:"):
                logger.info(f"    {line}")
            elif line.startswith("Backfilled fields:"):
                logger.info(f"    {line}")
            else:
                logger.info(f"    {line}")
    elif tool_name == "note":
        logger.info(f"  noted: {summary.primary}")
    elif tool_name == "finish":
        logger.info(f"  result: {summary.primary}")
    else:
        if not summary.success:
            logger.info(f"  result [FAIL]: {summary.primary}")
        else:
            logger.info("  result [OK]")
