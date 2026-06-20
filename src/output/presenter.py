"""Rich presentation-mode output for live demos."""

from __future__ import annotations

import sys
import time as _time
from pathlib import Path
from typing import Any, Protocol

from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.text import Text

from src.agent.loop_utils import summarize_tool_result
from src.agent.state import AgentState, rule_verdict_summary

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


class RunPresenter:
    """Rich-formatted live demo output on stdout."""

    active = True

    def __init__(self, console: Console | None = None, log_line_max_chars: int = 0):
        self._console = console or Console(
            file=sys.stdout,
            force_terminal=sys.stdout.isatty() if hasattr(sys.stdout, "isatty") else True,
        )
        self._log_line_max_chars = log_line_max_chars
        self._last_phase: str | None = None
        self._run_start_time: float | None = None
        self._page_count: int | None = None

    def batch_drive_start(self, total: int, folder_id: str) -> None:
        self._console.print()
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
        self._page_count = page_count
        name = Path(pdf_path).name
        body = name if page_count is None else f"{name}\n[cyan]{page_count}[/] pages"
        self._console.print()
        self._console.print(
            Panel(body, title="Invoice Compliance Agent", border_style="blue")
        )
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
        self._console.print(f"  [bold]▸[/] {label}")
        if reasoning and reasoning.strip():
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
            self._console.print(f"    {summary.primary}  [cyan]({elapsed})[/]")
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
            f"[bold {status_style}]Status: {status}[/]",
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
            lines.append(f"[red]Errors:[/] {', '.join(_rv['error_failed_rule_ids'])}")
        if _rv["warning_failed_rule_ids"]:
            lines.append(f"[yellow]Warnings:[/] {', '.join(_rv['warning_failed_rule_ids'])}")
        flagged = [k for k, v in state.extracted_fields.items() if v.flagged_for_review]
        if flagged:
            lines.append(f"[yellow]Review:[/] {', '.join(flagged)}")
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
