"""Tests for presentation mode and shared tool result summaries."""

from __future__ import annotations

import argparse
import io
import sys
from unittest.mock import patch

import pytest
from rich.console import Console

from src.agent.loop_utils import summarize_tool_result
from src.agent.state import AgentState, AgentStatus
from src.output.presenter import NullPresenter, RunPresenter, _tool_label


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
    assert "Invoice Compliance Agent" in out
    assert "travel_invoice.pdf" in out
    assert "SCAN" in out
    assert "EXTRACT" in out


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
