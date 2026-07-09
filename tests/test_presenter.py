"""Tests for presentation mode and shared tool result summaries."""

from __future__ import annotations

import argparse
import io
import sys
from unittest.mock import patch

import pytest
from rich.console import Console

from src.agent.loop_utils import summarize_tool_result
from src.agent.state import AgentState, AgentStatus, RuleResult
from src.output.presenter import ConfigLoadSummary, NullPresenter, RunPresenter, _tool_label


def test_summarize_inspect_file():
    summary = summarize_tool_result(
        "inspect_file",
        {"success": True, "page_count": 12, "size_mb": 2.4, "format": "PDF"},
        250,
    )
    assert summary.success is True
    assert "12 pages" in summary.primary
    assert "PDF" in summary.primary


def test_summarize_inventory_pages():
    summary = summarize_tool_result(
        "inventory_pages",
        {
            "success": True,
            "inventory": [
                {"page": 1, "category": "INVOICE_HEADER", "description": "Vendor header"},
                {"page": 2, "category": "LINE_ITEMS", "description": "Expense lines"},
            ],
        },
        250,
    )
    assert summary.primary == "2 page(s) mapped"
    assert len(summary.details) == 2
    assert summary.details[0].startswith("p1")


def test_summarize_classify_document_type():
    summary = summarize_tool_result(
        "classify_document_type",
        {"success": True, "invoice_type_id": "VIAJES", "confidence": 0.94},
        250,
    )
    assert "VIAJES" in summary.primary
    assert "94%" in summary.primary


def test_summarize_check_compliance():
    summary = summarize_tool_result(
        "check_compliance",
        {
            "success": True,
            "passed": 24,
            "failed_errors": ["RULE_A: missing field"],
            "failed_warnings": [],
            "skipped_checks": [],
            "visual_checks_pending": ["VIS_1"],
        },
        250,
    )
    assert "24 passed" in summary.primary
    assert any("ERROR:" in d for d in summary.details)
    assert any("Visual checks pending" in d for d in summary.details)


def test_tool_label_extract_with_page():
    label = _tool_label("extract_fields_vision", {"page_num": 2, "region": "header"})
    assert "page 2" in label
    assert "header" in label


def test_run_presenter_phase_banner():
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=120, color_system=None)
    presenter = RunPresenter(console=console)
    presenter.run_start("/tmp/travel_invoice.pdf")
    presenter.phase_change("SCAN")
    presenter.phase_change("EXTRACT")
    out = buf.getvalue()
    assert "Document" in out
    assert "travel_invoice.pdf" in out
    assert "SCAN" in out
    assert "EXTRACT" in out


def test_startup_context_shows_config_models_and_rule_groups():
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=140, color_system=None)
    presenter = RunPresenter(console=console)
    summary = ConfigLoadSummary(
        source="Google Drive config folder",
        path="/tmp/config",
        invoice_types=5,
        extraction_fields=62,
        compliance_rules=103,
        allowed_value_sets=7,
        denylist_phrases=3,
    )
    presenter.startup_context(
        summary,
        {
            "llm": {"provider": "openai"},
            "openai": {"reasoning_model": "o3-mini", "vision_model": "gpt-4.1-mini"},
            "agent": {"active_rule_groups": ["general"]},
        },
        ocr_enabled=True,
    )
    out = buf.getvalue()
    assert "Google Drive config folder" in out
    assert "5" in out
    assert "62" in out
    assert "103" in out
    assert "openai" in out
    assert "o3-mini" in out
    assert "gpt-4.1-mini" in out
    assert "general" in out
    assert "Surya enabled" in out


def test_run_presenter_tool_flow():
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=120, color_system=None)
    presenter = RunPresenter(console=console)
    presenter.phase_change("SCAN")
    presenter.tool_start(1, "inspect_file", "Checking file metadata", {})
    presenter.tool_result(
        "inspect_file",
        {"success": True, "page_count": 3, "size_mb": 1.0, "format": "PDF"},
        120,
    )
    out = buf.getvalue()
    assert "Inspecting file" in out
    assert "3 pages" in out
    assert "Checking file metadata" not in out


def test_run_presenter_can_show_reasoning_when_enabled():
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=120, color_system=None)
    presenter = RunPresenter(console=console, show_reasoning=True)
    presenter.tool_start(1, "inspect_file", "Checking file metadata", {})
    assert "Checking file metadata" in buf.getvalue()


def test_run_presenter_extract_retry_and_model_metadata():
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=140, color_system=None)
    presenter = RunPresenter(console=console)
    summary = ConfigLoadSummary("local config/csv", "config/csv", 5, 62, 103, 7, 3)
    presenter.startup_context(
        summary,
        {
            "llm": {"provider": "openai"},
            "openai": {"reasoning_model": "o3-mini", "vision_model": "gpt-4.1-mini"},
        },
        ocr_enabled=True,
    )
    params = {"page_num": 1, "region": "header"}
    presenter.tool_start(1, "extract_fields_vision", "long internal reasoning", params)
    presenter.tool_start(2, "extract_fields_vision", "long internal reasoning", params)
    out = buf.getvalue()
    assert "Model: gpt-4.1-mini" in out
    assert "OCR: Surya pre-pass" in out
    assert "Retry 2" in out
    assert "long internal reasoning" not in out


def test_null_presenter_is_inactive():
    assert NullPresenter.active is False


def test_presentation_cli_flag():
    with patch.object(sys, "argv", ["main.py", "--presentation", "--list-types"]):
        parser = argparse.ArgumentParser()
        parser.add_argument("--presentation", action="store_true")
        parser.add_argument("--list-types", action="store_true")
        args = parser.parse_args(sys.argv[1:])
        assert args.presentation is True


def test_run_complete_panel():
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=120, color_system=None)
    presenter = RunPresenter(console=console)
    state = AgentState(
        pdf_path="/tmp/invoice.pdf",
        output_dir="/tmp/out",
        status=AgentStatus.PASSED,
        turn=5,
    )
    presenter.run_complete(state, paths={"fields_csv": "/tmp/out/results.csv"})
    out = buf.getvalue()
    assert "Result" in out
    assert "PASSED" in out
    assert "results.csv" in out


def test_finish_result_uses_computed_error_list_not_reasoning_text():
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=140, color_system=None)
    presenter = RunPresenter(console=console)
    presenter.tool_result(
        "finish",
        {
            "finished": True,
            "status": "needs_review",
            "error_failures": ["R_VIA_002"],
            "warning_failures": ["R_VIA_004"],
            "status_explanation": "Run status reflects blocking error-severity rule failures.",
        },
        1200,
    )
    out = buf.getvalue()
    assert "Decision: NEEDS_REVIEW" in out
    assert "Blocking errors:" in out
    assert "R_VIA_002" in out
    assert "status=needs_review" not in out


def test_run_complete_panel_lists_blocking_errors():
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=140, color_system=None)
    presenter = RunPresenter(console=console)
    state = AgentState(
        pdf_path="/tmp/invoice.pdf",
        output_dir="/tmp/out",
        status=AgentStatus.NEEDS_REVIEW,
        turn=5,
        finish_reason="all blocking errors resolved",
    )
    state.rule_results = [
        RuleResult(
            rule_id="R_VIA_002",
            rule_name="total_required",
            field_id="F_TOTAL",
            status="failed",
            severity="error",
            message="Missing total",
        )
    ]
    presenter.run_complete(state, paths={"fields_csv": "/tmp/out/results.csv"})
    out = buf.getvalue()
    assert "Decision: NEEDS_REVIEW" in out
    assert "Blocking errors:" in out
    assert "R_VIA_002" in out
