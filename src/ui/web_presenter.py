"""PresenterProtocol implementation that turns agent callbacks into browser events."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.agent.loop_utils import summarize_tool_result
from src.agent.state import AgentState
from src.llm.config_resolve import (
    active_rule_groups_from_config,
    llm_provider_name,
    reasoning_model_for_config,
    vision_model_for_config,
)
from src.output.presenter import (
    PHASE_LABELS,
    _VALIDATE_TOOLS,
    _VISION_TOOLS,
    ConfigLoadSummary,
    _tool_intent,
    _tool_key,
    _tool_label,
)
from src.ui.events import RunHub
from src.ui.rule_summary import (
    RuleCatalog,
    finish_rows,
    friendly_finish_explanation,
    friendly_finish_reason,
    summarize_compliance,
    summarize_run,
    summarize_visual,
)


def _detail(text: str) -> dict[str, str]:
    """Mirror the terminal presenter's ERROR:/WARN: colouring as a level field."""
    if text.startswith("ERROR:"):
        return {"text": text, "level": "error"}
    if text.startswith("WARN:"):
        return {"text": text, "level": "warn"}
    return {"text": text, "level": "info"}


class WebPresenter:
    """
    One instance lives on the (single) agent; the runner points it at the current run's hub
    with `attach` / `detach`. Callbacks arriving while no hub is attached are dropped.
    """

    active = True

    def __init__(self) -> None:
        self._hub: RunHub | None = None
        self._models: dict[str, str] = {}
        self._ocr_enabled: bool | None = None
        self._reasoning_model = ""
        self._vision_model = ""
        self._last_phase: str | None = None
        self._tool_counts: dict[tuple[Any, ...], int] = {}
        self._tool_seq = 0
        self._current_tool_id = 0
        self._catalog = RuleCatalog()

    def set_rule_catalog(self, catalog: RuleCatalog) -> None:
        """Lets the page show rule names instead of ids (built from the loaded config)."""
        self._catalog = catalog

    # --- run binding -----------------------------------------------------------------

    def attach(self, hub: RunHub) -> None:
        self._hub = hub
        self._last_phase = None
        self._tool_counts = {}
        self._tool_seq = 0
        self._current_tool_id = 0

    def detach(self) -> None:
        self._hub = None

    def _emit(self, event_type: str, **data: Any) -> None:
        if self._hub is not None:
            self._hub.publish(event_type, **data)

    # --- PresenterProtocol -----------------------------------------------------------

    def startup_context(
        self,
        config_summary: ConfigLoadSummary,
        app_config: dict,
        *,
        ocr_enabled: bool | None = None,
    ) -> None:
        self._reasoning_model = reasoning_model_for_config(app_config)
        self._vision_model = vision_model_for_config(app_config)
        self._ocr_enabled = ocr_enabled
        self._models = {
            "provider": llm_provider_name(app_config),
            "reasoning": self._reasoning_model,
            "vision": self._vision_model,
            "ocr": "enabled" if ocr_enabled else "disabled" if ocr_enabled is False else "auto",
            "rule_groups": ", ".join(active_rule_groups_from_config(app_config)),
            "invoice_types": config_summary.invoice_types,
            "compliance_rules": config_summary.compliance_rules,
        }

    def batch_drive_start(self, total: int, folder_id: str) -> None:
        pass

    def batch_drive_item(self, idx: int, total: int, name: str) -> None:
        pass

    def run_start(self, pdf_path: str, page_count: int | None = None) -> None:
        self._emit(
            "run_started",
            filename=Path(pdf_path).name,
            page_count=page_count,
            models=self._models,
        )

    def phase_change(self, phase: str) -> None:
        if phase == self._last_phase:
            return
        self._last_phase = phase
        self._emit("phase", phase=phase, label=PHASE_LABELS.get(phase, phase))

    def tool_start(
        self, turn: int, tool: str, reasoning: str, params: dict | None = None
    ) -> None:
        if tool in _VALIDATE_TOOLS and self._last_phase != "VALIDATE":
            self.phase_change("VALIDATE")
        key = _tool_key(tool, params)
        self._tool_counts[key] = self._tool_counts.get(key, 0) + 1
        self._tool_seq += 1
        self._current_tool_id = self._tool_seq

        if tool in _VISION_TOOLS:
            model = self._vision_model
        else:
            model = self._reasoning_model
        ocr = bool(tool == "extract_fields_vision" and self._ocr_enabled)
        self._emit(
            "tool_start",
            id=self._current_tool_id,
            turn=turn,
            tool=tool,
            label=_tool_label(tool, params),
            intent=_tool_intent(tool, params),
            model=model,
            ocr=ocr,
            attempt=self._tool_counts[key],
            page_num=(params or {}).get("page_num"),
        )

    def tool_result(self, tool: str, result: Any, elapsed_ms: int) -> None:
        rules: list[dict[str, str]] = []
        if tool == "finish":
            primary, details, rules, success = self._finish_summary(result)
        elif tool in ("check_compliance", "check_compliance_visual") and isinstance(result, dict):
            summarize = summarize_compliance if tool == "check_compliance" else summarize_visual
            primary, rules, extra = summarize(result, self._catalog)
            details = [_detail(d["text"]) if d["level"] == "info" else d for d in extra]
            success = bool(result.get("success", True))
        else:
            summary = summarize_tool_result(tool, result, 0)
            primary = summary.primary
            details = [_detail(line) for line in summary.details]
            success = summary.success
            if tool == "classify_document_type" and success:
                primary = f"Document type: {primary}"
        self._emit(
            "tool_result",
            id=self._current_tool_id,
            tool=tool,
            success=success,
            primary=primary,
            details=details,
            rules=rules,
            elapsed_ms=elapsed_ms,
        )

    def _finish_summary(self, result: Any) -> tuple[str, list[dict[str, str]], list[dict[str, str]], bool]:
        if not isinstance(result, dict):
            return str(result), [], [], True
        status = str(result.get("status") or "unknown").replace("_", " ").upper()
        details: list[dict[str, str]] = []
        explanation = str(result.get("status_explanation") or "").strip()
        if explanation:
            details.append(_detail(friendly_finish_explanation(explanation)))
        return f"Decision: {status}", details, finish_rows(self._catalog, result), True

    def run_complete(
        self,
        state: AgentState,
        *,
        paths: dict[str, str],
        ground_truth_score: dict | None = None,
        learn: bool = False,
        learn_ran: bool = False,
        no_ground_truth: bool = False,
        ground_truth_csv_only: bool = False,
    ) -> None:
        self._emit(
            "run_complete",
            status=state.status.value,
            finish_reason=friendly_finish_reason(state.finish_reason or ""),
            turns=state.turn,
            fields_extracted=len(state.extracted_fields),
            rules=summarize_run(state.rule_results, self._catalog),
            flagged_fields=[k for k, v in state.extracted_fields.items() if v.flagged_for_review],
            outputs=dict(paths),
        )
