"""
Fixed pipeline orchestration: deterministic tool sequence with the same primitives as the loop.
Vision/classify/extract still call the LLM inside tools; there is no outer reasoning loop.
"""

from __future__ import annotations

import logging
import time as _time
from pathlib import Path
from typing import Any, Callable

from src.agent.agent_settings import clip_for_log
from src.agent.state import AgentState, AgentStatus
from src.output.presenter import PresenterProtocol, NullPresenter

logger = logging.getLogger(__name__)

_CATEGORY_REGION = {
    "INVOICE_HEADER": "header",
    "LINE_ITEMS": "line_items",
    "TOTALS": "totals",
    "SIGNATURE_STAMP": "body",
    "SUPPORTING_DOC": "body",
    "COVER_PAGE": "body",
    "BLANK": "body",
}


def _dispatch(
    state: AgentState,
    tools: dict[str, Callable],
    tool_name: str,
    params: dict,
    reasoning: str,
    log_handle: Any,
    log_line_max_chars: int,
    *,
    last_phase: list[str | None],
    log_turn_start: Callable[..., None] | None = None,
    log_tool_result_fn: Callable[..., None] | None = None,
    presenter: PresenterProtocol | None = None,
) -> Any:
    turn_start = _time.monotonic()
    if log_turn_start is not None:
        log_turn_start(state, last_phase, tool_name, reasoning, params)
    elif presenter and presenter.active:
        from src.agent.phases import current_phase

        phase = current_phase(state)
        if phase != last_phase[0]:
            presenter.phase_change(phase)
            last_phase[0] = phase
        presenter.tool_start(state.turn, tool_name, reasoning, params)
    else:
        logger.info(
            f"Pipeline | {tool_name} | {clip_for_log(reasoning, log_line_max_chars)}"
        )
    result = tools[tool_name](state, **params)
    elapsed_ms = int((_time.monotonic() - turn_start) * 1000)
    state.record_action(tool_name, params, result, reasoning)
    try:
        from src.agent.loop_utils import append_log_entry

        append_log_entry(
            log_handle,
            {
                "turn": state.turn - 1,
                "timestamp": _time.strftime("%Y-%m-%dT%H:%M:%S"),
                "tool": tool_name,
                "params": params,
                "reasoning": reasoning,
                "result": result,
                "elapsed_ms": elapsed_ms,
                "orchestration": "pipeline",
            },
        )
    except Exception:
        pass
    if log_tool_result_fn is not None:
        log_tool_result_fn(tool_name, result, elapsed_ms)
    else:
        from src.agent.loop_utils import log_tool_result

        log_tool_result(tool_name, result, log_line_max_chars)
    return result


def run_fixed_pipeline(
    *,
    state: AgentState,
    tools: dict[str, Callable],
    log_handle: Any,
    log_line_max_chars: int,
    planning_enabled: bool,
    generate_plan_fn: Callable[[AgentState], list] | None,
    presenter: PresenterProtocol | None = None,
    log_turn_start: Callable[..., None] | None = None,
    log_tool_result_fn: Callable[..., None] | None = None,
) -> None:
    """
    Mutates `state` until finish, error, or unrecoverable tool failure.
    Caller opens log_handle and sets state.run_log_path.
    """
    last_phase: list[str | None] = [None]
    pres = presenter or NullPresenter()

    def _run(name: str, params: dict | None = None, reason: str = "") -> Any:
        p = params or {}
        return _dispatch(
            state,
            tools,
            name,
            p,
            reason or f"pipeline:{name}",
            log_handle,
            log_line_max_chars,
            last_phase=last_phase,
            log_turn_start=log_turn_start,
            log_tool_result_fn=log_tool_result_fn,
            presenter=pres,
        )

    _run("inspect_file", {}, "pipeline: file metadata")
    if state.status != AgentStatus.RUNNING:
        return

    r = _run("compress_pages", {"dpi": 48, "quality": 30}, "pipeline: thumbnails for inventory")
    if isinstance(r, dict) and r.get("success") is False:
        state.status = AgentStatus.ERROR
        state.finish_reason = r.get("error", "compress_pages failed")
        return

    r = _run("inventory_pages", {}, "pipeline: page inventory")
    if isinstance(r, dict) and r.get("success") is False:
        state.status = AgentStatus.ERROR
        state.finish_reason = r.get("error", "inventory_pages failed")
        return

    if not state.invoice_type_id:
        r = _run("classify_document_type", {}, "pipeline: document type")
        if isinstance(r, dict) and r.get("success") is False:
            state.status = AgentStatus.ERROR
            state.finish_reason = r.get("error", "classify_document_type failed")
            return

    if planning_enabled and generate_plan_fn and state.invoice_type_id and not state.execution_plan:
        state.execution_plan = generate_plan_fn(state)

    _dpi = int(getattr(state, "page_render_dpi", 150) or 150)
    r = _run("convert_pdf_to_images", {"dpi": _dpi}, "pipeline: full-quality render")
    if isinstance(r, dict) and r.get("success") is False:
        state.status = AgentStatus.ERROR
        state.finish_reason = r.get("error", "convert_pdf_to_images failed")
        return

    n_pages = len(state.page_image_paths)
    if n_pages <= 0:
        state.status = AgentStatus.ERROR
        state.finish_reason = "No pages rendered after convert_pdf_to_images"
        return

    inv_by_page: dict[int, str] = {}
    for e in state.page_inventory or []:
        try:
            inv_by_page[int(e["page"])] = str(e.get("category") or "")
        except (KeyError, TypeError, ValueError):
            continue

    extracted_pages: set[int] = set()
    for page_num in range(1, n_pages + 1):
        cat = inv_by_page.get(page_num, "")
        region = _CATEGORY_REGION.get(cat.upper() if cat else "", "body")
        reason = f"pipeline: extract page {page_num} region={region}"
        r = _run(
            "extract_fields_vision",
            {"page_num": page_num, "region": region},
            reason,
        )
        if isinstance(r, dict) and r.get("success"):
            extracted_pages.add(page_num)

    if not extracted_pages:
        # Fallback: at least one full pass on first page
        _run("extract_fields_vision", {"page_num": 1, "region": "header"}, "pipeline: fallback header p1")
        _run("extract_fields_vision", {"page_num": 1, "region": "body"}, "pipeline: fallback body p1")

    r = _run("check_compliance", {}, "pipeline: field rules")
    if isinstance(r, dict) and r.get("success") is False and r.get("error"):
        logger.warning(f"check_compliance issue: {r.get('error')}")

    guard = 0
    while state.visual_checks_pending and guard < 8:
        guard += 1
        page_num = 1
        for e in state.page_inventory or []:
            if str(e.get("category", "")).upper() in ("SIGNATURE_STAMP", "INVOICE_HEADER"):
                try:
                    page_num = int(e["page"])
                except (TypeError, ValueError):
                    page_num = 1
                break
        r = _run(
            "check_compliance_visual",
            {"page_num": page_num},
            f"pipeline: visual compliance page {page_num}",
        )
        if isinstance(r, dict) and r.get("success") is False:
            logger.warning(f"check_compliance_visual: {r.get('error')}")
            break

    res = _run(
        "finish",
        {"reason": "pipeline_complete", "all_errors_resolved": True},
        "pipeline: finish",
    )
    if isinstance(res, dict) and res.get("finished") is False:
        state.status = AgentStatus.ERROR
        state.finish_reason = str(res.get("error", "finish rejected"))
