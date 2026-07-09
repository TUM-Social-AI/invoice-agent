"""Rich presentation-mode output for live demos."""

from __future__ import annotations

import sys
import time as _time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.text import Text

from src.agent.loop_utils import summarize_tool_result
from src.agent.state import AgentState, rule_verdict_summary
from src.llm.config_resolve import (
    active_rule_groups_from_config,
    llm_provider_name,
    reasoning_model_for_config,
    vision_model_for_config,
)


@dataclass(frozen=True)
class ConfigLoadSummary:
    source: str
    path: str
    invoice_types: int
    extraction_fields: int
    compliance_rules: int
    allowed_value_sets: int
    denylist_phrases: int

PHASE_LABELS: dict[str, str] = {
    "SCAN": "Understanding the document",
    "EXTRACT": "Pulling structured data",
    "VALIDATE": "Visual checks & finish",
}

TOOL_LABELS: dict[str, str] = {
    "inspect_file": "Inspecting file",
    "compress_pages": "Building page map (low-res scan)",
    "inventory_pages": "Mapping pages",
    "classify_document_type": "Classifying document type",
    "convert_pdf_to_images": "Rendering full-quality pages",
    "extract_fields_vision": "Extracting fields",
    "crop_region": "Cropping region",
    "check_compliance": "Running compliance checks",
    "check_compliance_visual": "Visual compliance (stamps, signatures)",
    "read_learnings": "Loading prior learnings",
    "write_learning": "Writing learning",
    "edit_learning": "Editing learning",
    "delete_learning": "Deleting learning",
    "note": "Agent note",
    "flag_for_human_review": "Flagging for human review",
    "finish": "Finishing run",
    "install_package": "Installing package",
}


class PresenterProtocol(Protocol):
    @property
    def active(self) -> bool: ...

    def startup_context(
        self,
        config_summary: ConfigLoadSummary,
        app_config: dict,
        *,
        ocr_enabled: bool | None = None,
    ) -> None: ...

    def batch_drive_start(self, total: int, folder_id: str) -> None: ...

    def batch_drive_item(self, idx: int, total: int, name: str) -> None: ...

    def run_start(self, pdf_path: str, page_count: int | None = None) -> None: ...

    def phase_change(self, phase: str) -> None: ...

    def tool_start(
        self, turn: int, tool: str, reasoning: str, params: dict | None = None
    ) -> None: ...

    def tool_result(self, tool: str, result: Any, elapsed_ms: int) -> None: ...

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
    ) -> None: ...


class NullPresenter:
    """No-op presenter used when presentation mode is off."""

    active = False

    def startup_context(
        self,
        config_summary: ConfigLoadSummary,
        app_config: dict,
        *,
        ocr_enabled: bool | None = None,
    ) -> None:
        pass

    def batch_drive_start(self, total: int, folder_id: str) -> None:
        pass

    def batch_drive_item(self, idx: int, total: int, name: str) -> None:
        pass

    def run_start(self, pdf_path: str, page_count: int | None = None) -> None:
        pass

    def phase_change(self, phase: str) -> None:
        pass

    def tool_start(
        self, turn: int, tool: str, reasoning: str, params: dict | None = None
    ) -> None:
        pass

    def tool_result(self, tool: str, result: Any, elapsed_ms: int) -> None:
        pass

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
        pass


_VALIDATE_TOOLS = {"check_compliance", "check_compliance_visual", "finish"}
_VISION_TOOLS = {
    "inventory_pages",
    "classify_document_type",
    "extract_fields_vision",
    "check_compliance_visual",
}


def _tool_label(tool: str, params: dict | None) -> str:
    base = TOOL_LABELS.get(tool, tool.replace("_", " ").title())
    if tool == "extract_fields_vision" and params:
        page = params.get("page_num")
        region = params.get("region")
        if page is not None:
            suffix = f" — page {page}"
            if region:
                suffix += f" ({region})"
            return base + suffix
    return base


def _format_elapsed(ms: int) -> str:
    if ms < 1000:
        return f"{ms}ms"
    return f"{ms / 1000:.1f}s"


def _tool_key(tool: str, params: dict | None) -> tuple[Any, ...]:
    params = params or {}
    return (
        tool,
        params.get("page_num"),
        params.get("region"),
    )


def _tool_intent(tool: str, params: dict | None) -> str:
    params = params or {}
    if tool == "extract_fields_vision":
        page = params.get("page_num")
        region = params.get("region")
        if page is not None and region:
            return f"Reading {region} fields from page {page}"
        if page is not None:
            return f"Reading fields from page {page}"
    if tool == "inventory_pages":
        return "Classifying each page by document role"
    if tool == "classify_document_type":
        return "Choosing the matching invoice rule set"
    if tool == "check_compliance":
        return "Comparing extracted fields against active rules"
    if tool == "check_compliance_visual":
        page = params.get("page_num")
        if page is not None:
            return f"Checking visual evidence on page {page}"
        return "Checking stamps, signatures, and visual evidence"
    if tool == "finish":
        return "Computing final decision from rule state"
    return ""


def _remaining_gaps_from_compliance(result: Any) -> list[str]:
    if not isinstance(result, dict):
        return []
    gaps: set[str] = set()
    for skipped in result.get("skipped_checks", []) or []:
        reason = str(skipped.get("reason", ""))
        for marker in ("field(s):", "missing values):"):
            if marker in reason:
                tail = reason.split(marker, 1)[1]
                for raw in tail.replace(".", "").split(","):
                    val = raw.strip()
                    if val and " " not in val:
                        gaps.add(val)
    return sorted(gaps)


def _finish_lines(result: Any, elapsed: str) -> list[str]:
    if not isinstance(result, dict):
        return [f"{result}  [cyan]({elapsed})[/]"]
    status = str(result.get("status") or "unknown").upper()
    errors = result.get("error_failures", []) or []
    warnings = result.get("warning_failures", []) or []
    explanation = str(result.get("status_explanation") or "").strip()
    lines = [f"    Decision: [bold]{status}[/]  [cyan]({elapsed})[/]"]
    if errors:
        lines.append(f"      [red]Blocking errors:[/] {', '.join(str(e) for e in errors)}")
    if warnings:
        lines.append(f"      [yellow]Warnings:[/] {', '.join(str(w) for w in warnings)}")
    if explanation:
        lines.append(f"      Reason: {explanation}")
    return lines


class RunPresenter:
    """Rich-formatted live demo output on stdout."""

    active = True

    def __init__(
        self,
        console: Console | None = None,
        log_line_max_chars: int = 0,
        *,
        show_reasoning: bool = False,
    ):
        self._console = console or Console(
            file=sys.stdout,
            force_terminal=sys.stdout.isatty() if hasattr(sys.stdout, "isatty") else True,
        )
        self._log_line_max_chars = log_line_max_chars
        self._show_reasoning = show_reasoning
        self._last_phase: str | None = None
        self._run_start_time: float | None = None
        self._page_count: int | None = None
        self._tool_counts: dict[tuple[Any, ...], int] = {}
        self._reasoning_model: str = ""
        self._vision_model: str = ""
        self._ocr_enabled: bool | None = None

    def startup_context(
        self,
        config_summary: ConfigLoadSummary,
        app_config: dict,
        *,
        ocr_enabled: bool | None = None,
    ) -> None:
        provider = llm_provider_name(app_config)
        self._reasoning_model = reasoning_model_for_config(app_config)
        self._vision_model = vision_model_for_config(app_config)
        self._ocr_enabled = ocr_enabled
        rule_groups = ", ".join(active_rule_groups_from_config(app_config))
        ocr_status = "enabled" if ocr_enabled else "disabled" if ocr_enabled is False else "auto"
        body = "\n".join([
            f"[bold]Config:[/] {config_summary.source}",
            f"Path: {config_summary.path}",
            (
                f"Loaded: [cyan]{config_summary.invoice_types}[/] invoice types · "
                f"[cyan]{config_summary.extraction_fields}[/] fields · "
                f"[cyan]{config_summary.compliance_rules}[/] rules · "
                f"[cyan]{config_summary.allowed_value_sets}[/] allowed-value sets"
            ),
            f"Active rule groups: {rule_groups}",
            (
                f"Models: [bold]{provider}[/] · reasoning [cyan]{self._reasoning_model}[/] · "
                f"vision [cyan]{self._vision_model}[/]"
            ),
            f"OCR: Surya {ocr_status}",
        ])
        self._console.print()
        self._console.print(Panel(body, title="Invoice Compliance Agent", border_style="blue"))
        self._console.print()

    def batch_drive_start(self, total: int, folder_id: str) -> None:
        self._console.print(
            Panel(
                f"[bold]Google Drive[/] · [cyan]{total}[/] PDF{'s' if total != 1 else ''} found\n"
                f"{folder_id}",
                title="Invoice Compliance Agent",
                border_style="blue",
            )
        )
        self._console.print()

    def batch_drive_item(self, idx: int, total: int, name: str) -> None:
        self._console.print(Rule(f"[bold blue]{idx}/{total}[/]  {name}", style="blue"))
        self._console.print()

    def run_start(self, pdf_path: str, page_count: int | None = None) -> None:
        self._run_start_time = _time.monotonic()
        self._last_phase = None
        self._tool_counts = {}
        self._page_count = page_count
        name = Path(pdf_path).name
        body = name if page_count is None else f"{name}\n[cyan]{page_count}[/] pages"
        self._console.print(Rule(f"[bold blue]Document[/]  {body}", style="blue"))
        self._console.print()

    def phase_change(self, phase: str) -> None:
        if phase == self._last_phase:
            return
        self._last_phase = phase
        label = PHASE_LABELS.get(phase, phase)
        self._console.print()
        self._console.print(Rule(f"[bold blue]{phase}[/]  {label}", style="blue"))
        self._console.print()

    def tool_start(
        self, turn: int, tool: str, reasoning: str, params: dict | None = None
    ) -> None:
        if tool in _VALIDATE_TOOLS and self._last_phase != "VALIDATE":
            self.phase_change("VALIDATE")
        label = _tool_label(tool, params)
        key = _tool_key(tool, params)
        self._tool_counts[key] = self._tool_counts.get(key, 0) + 1
        attempt = self._tool_counts[key]
        if attempt > 1:
            label += f" · Retry {attempt}"
        self._console.print(f"  [bold]▸[/] {label}")
        intent = _tool_intent(tool, params)
        if intent:
            self._console.print(f"    {intent}")
        if tool in _VISION_TOOLS and self._vision_model:
            self._console.print(f"    Model: {self._vision_model}")
            if tool == "extract_fields_vision" and self._ocr_enabled:
                self._console.print("    OCR: Surya pre-pass")
        elif self._reasoning_model and tool not in _VISION_TOOLS:
            self._console.print(f"    Model: {self._reasoning_model}")
        if self._show_reasoning and reasoning and reasoning.strip():
            reason = reasoning.strip()
            if self._log_line_max_chars > 0:
                reason = reason[: self._log_line_max_chars]
            self._console.print(f"    [italic]{reason}[/]")

    def tool_result(self, tool: str, result: Any, elapsed_ms: int) -> None:
        summary = summarize_tool_result(tool, result, self._log_line_max_chars)
        elapsed = _format_elapsed(elapsed_ms)

        if tool == "classify_document_type" and summary.success:
            self._console.print(
                f"    [green]✓[/] Document type: [bold]{summary.primary}[/]  [cyan]({elapsed})[/]"
            )
            return

        if tool == "finish":
            for line in _finish_lines(result, elapsed):
                self._console.print(line)
            return

        style = "green" if summary.success else "red"
        marker = "✓" if summary.success else "✗"
        self._console.print(f"    [{style}]{marker}[/] {summary.primary}  [cyan]({elapsed})[/]")

        for line in summary.details:
            if line.startswith("ERROR:"):
                self._console.print(f"      [red]{line}[/]")
            elif line.startswith("WARN:"):
                self._console.print(f"      [yellow]{line}[/]")
            else:
                self._console.print(f"      {line}")
        if tool == "check_compliance":
            gaps = _remaining_gaps_from_compliance(result)
            if gaps:
                self._console.print(f"      Remaining gaps: {', '.join(gaps)}")

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
        elapsed_s = ""
        if self._run_start_time is not None:
            elapsed_s = f" · [cyan]{_format_elapsed(int((_time.monotonic() - self._run_start_time) * 1000))}[/]"

        _rv = rule_verdict_summary(state.rule_results)
        status = state.status.value.upper()
        status_style = {
            "PASSED": "green",
            "NEEDS_REVIEW": "yellow",
            "FAILED": "red",
            "ERROR": "red",
            "INTERRUPTED": "yellow",
        }.get(status, "white")

        n_fields = len(state.extracted_fields)
        n_turns = state.turn
        n_passed = len(state.passed_rules)
        n_errors = len(_rv["error_failed_rule_ids"])
        n_warnings = len(_rv["warning_failed_rule_ids"])

        lines = [
            f"[bold {status_style}]Decision: {status}[/]",
            (
                f"[cyan]{n_fields}[/] fields extracted · "
                f"[cyan]{n_turns}[/] turns{elapsed_s}"
            ),
            (
                f"[cyan]{n_passed}[/] rules passed · "
                f"[{'red' if n_errors else 'white'}]{n_errors} blocking error(s)[/] · "
                f"[{'yellow' if n_warnings else 'white'}]{n_warnings} warning(s)[/]"
            ),
        ]
        if _rv["error_failed_rule_ids"]:
            lines.append(f"[red]Blocking errors:[/] {', '.join(_rv['error_failed_rule_ids'])}")
        if _rv["warning_failed_rule_ids"]:
            lines.append(f"[yellow]Warnings:[/] {', '.join(_rv['warning_failed_rule_ids'])}")
        flagged = [k for k, v in state.extracted_fields.items() if v.flagged_for_review]
        if flagged:
            lines.append(f"[yellow]Review:[/] {', '.join(flagged)}")
        if state.finish_reason:
            lines.append(f"Reason: {state.finish_reason}")
        lines.append(f"Output: {paths.get('fields_csv', '')}")

        if ground_truth_score:
            s = ground_truth_score
            ft = s.get("fields_total") or 0
            if ft:
                gt_line = (
                    f"Ground truth: [cyan]{ft}[/] field(s) · "
                    f"exact [cyan]{s.get('fields_exact', 0)}[/] · "
                    f"partial [cyan]{s.get('fields_partial', 0)}[/] · "
                    f"wrong [cyan]{s.get('fields_wrong', 0)}[/]"
                )
                if s.get("field_accuracy") is not None:
                    exa = s.get("exact_accuracy")
                    gt_line += (
                        f" · lenient [cyan]{s['field_accuracy']:.0%}[/]"
                        f" · strict [cyan]{(exa if exa is not None else 0):.0%}[/]"
                    )
                lines.append(gt_line)
            if s.get("rule_accuracy") is not None:
                rt = s.get("rules_total") or 0
                lines.append(
                    f"Compliance vs truth: [cyan]{s.get('rules_correct', 0)}/{rt}[/] "
                    f"([cyan]{s['rule_accuracy']:.0%}[/])"
                )
        elif no_ground_truth and learn:
            stem = Path(state.pdf_path).stem
            lines.append(f"Learn: no ground truth (add {stem}_truth.json)")
        elif ground_truth_csv_only:
            lines.append("Ground truth: none for this file")

        if learn_ran:
            lines.append("Learnings written to learnings.md")

        body = Text.from_markup("\n".join(lines))
        self._console.print()
        self._console.print(Panel(body, title="Result", border_style=status_style))
        self._console.print()
