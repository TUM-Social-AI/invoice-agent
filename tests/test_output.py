"""
Tests for output writer and learnings read/write.
Run with: pytest tests/test_output.py -v
"""

import csv
import tempfile
from pathlib import Path

import pytest

from src.agent.state import AgentState, AgentStatus, FieldResult, RuleResult
from src.output.writer import write_canonical_results, write_results
from src.tools.tools import read_learnings, write_learning


def make_complete_state() -> AgentState:
    state = AgentState(
        pdf_path="/invoices/test_invoice.pdf",
        invoice_type_id="EU_VAT",
        output_dir="/tmp/test",
    )
    state.status = AgentStatus.PASSED
    state.turn = 8
    state.finish_reason = "compliance_passed"

    state.extracted_fields = {
        "vendor_name": FieldResult(
            field_id="EU_VAT_001", field_name="vendor_name",
            extracted_value="Acme GmbH", confidence=0.97,
            source_page=1, source_region="header",
        ),
        "vendor_vat_id": FieldResult(
            field_id="EU_VAT_002", field_name="vendor_vat_id",
            extracted_value="DE123456789", confidence=0.92,
            source_page=1, source_region="header",
        ),
        "gross_amount": FieldResult(
            field_id="EU_VAT_010", field_name="gross_amount",
            extracted_value=1190.00, confidence=0.88,
            source_page=2, source_region="totals",
        ),
    }
    state.rule_results = [
        RuleResult(
            rule_id="R_EU_001", rule_name="vendor_vat_required",
            field_id="EU_VAT_002", status="passed", severity="error", message="OK",
        ),
        RuleResult(
            rule_id="R_EU_007", rule_name="vat_rate_plausible",
            field_id="EU_VAT_008", status="failed", severity="warning",
            message="VAT rate is outside the expected range",
        ),
    ]
    state.passed_rules = ["R_EU_001"]
    state.failed_rules = []
    return state


class TestWriteResults:
    def test_creates_fields_csv(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = make_complete_state()
            paths = write_results(state, tmpdir)
            assert Path(paths["fields_csv"]).exists()

    def test_creates_compliance_csv(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = make_complete_state()
            paths = write_results(state, tmpdir)
            assert Path(paths["compliance_csv"]).exists()

    def test_creates_summary_csv(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = make_complete_state()
            paths = write_results(state, tmpdir)
            assert Path(paths["summary_csv"]).exists()

    def test_fields_csv_content(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = make_complete_state()
            paths = write_results(state, tmpdir)
            with open(paths["fields_csv"], newline="") as f:
                rows = list(csv.DictReader(f))
            assert len(rows) == 3
            vendors = [r for r in rows if r["field_name"] == "vendor_name"]
            assert len(vendors) == 1
            assert vendors[0]["extracted_value"] == "Acme GmbH"
            assert vendors[0]["confidence"] == "0.97"

    def test_compliance_csv_content(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = make_complete_state()
            paths = write_results(state, tmpdir)
            with open(paths["compliance_csv"], newline="") as f:
                rows = list(csv.DictReader(f))
            assert len(rows) == 2
            passed = [r for r in rows if r["rule_id"] == "R_EU_001"]
            assert passed[0]["status"] == "passed"
            warned = [r for r in rows if r["rule_id"] == "R_EU_007"]
            assert warned[0]["status"] == "failed"
            assert warned[0]["severity"] == "warning"

    def test_summary_appends_multiple_runs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = make_complete_state()
            write_results(state, tmpdir)
            write_results(state, tmpdir)
            summary_path = Path(tmpdir) / "summary.csv"
            with open(summary_path, newline="") as f:
                rows = list(csv.DictReader(f))
            assert len(rows) == 2

    def test_flagged_fields_recorded(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = make_complete_state()
            state.extracted_fields["vendor_vat_id"].flagged_for_review = True
            state.extracted_fields["vendor_vat_id"].review_reason = "Could not extract after 3 attempts"
            paths = write_results(state, tmpdir)
            with open(paths["fields_csv"], newline="") as f:
                rows = list(csv.DictReader(f))
            vat_row = next(r for r in rows if r["field_name"] == "vendor_vat_id")
            assert vat_row["flagged_for_review"] == "True"
            assert "3 attempts" in vat_row["review_reason"]

    def test_write_canonical_results_wrapper_coexists_with_legacy_outputs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = make_complete_state()
            legacy_paths = write_results(state, tmpdir)
            canonical_paths = write_canonical_results([state], tmpdir)

            assert set(legacy_paths) == {"fields_csv", "compliance_csv", "summary_csv"}
            assert Path(canonical_paths["invoice_summary_csv"]).name == "invoice_summary.csv"
            assert Path(canonical_paths["compliance_results_csv"]).name == "compliance_results.csv"
            assert Path(legacy_paths["summary_csv"]).exists()
            assert Path(canonical_paths["invoice_summary_csv"]).exists()



class TestLearnings:
    def test_write_and_read_learning(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = f"{tmpdir}/learnings.md"
            write_learning("EU_VAT", "extraction_patterns", "Test pattern note", learnings_path=path)
            content = read_learnings("EU_VAT", learnings_path=path)
            assert "Test pattern note" in content
            assert "extraction_patterns" in content

    def test_multiple_categories(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = f"{tmpdir}/learnings.md"
            write_learning("EU_VAT", "extraction_patterns", "Pattern A", learnings_path=path)
            write_learning("EU_VAT", "common_failures", "Failure B", learnings_path=path)
            content = read_learnings("EU_VAT", learnings_path=path)
            assert "Pattern A" in content
            assert "Failure B" in content

    def test_multiple_invoice_types_isolated(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = f"{tmpdir}/learnings.md"
            write_learning("EU_VAT", "extraction_patterns", "EU only note", learnings_path=path)
            write_learning("CUSTOMS", "extraction_patterns", "Customs only note", learnings_path=path)
            eu_content = read_learnings("EU_VAT", learnings_path=path)
            cust_content = read_learnings("CUSTOMS", learnings_path=path)
            assert "EU only note" in eu_content
            assert "Customs only note" not in eu_content
            assert "Customs only note" in cust_content
            assert "EU only note" not in cust_content

    def test_append_to_existing_category(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = f"{tmpdir}/learnings.md"
            write_learning("EU_VAT", "extraction_patterns", "First note", learnings_path=path)
            write_learning("EU_VAT", "extraction_patterns", "Second note", learnings_path=path)
            content = read_learnings("EU_VAT", learnings_path=path)
            assert "First note" in content
            assert "Second note" in content

    def test_unknown_type_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = f"{tmpdir}/learnings.md"
            result = read_learnings("NONEXISTENT", learnings_path=path)
            assert result == ""

    def test_nonexistent_file_returns_empty(self):
        result = read_learnings("EU_VAT", learnings_path="/tmp/does_not_exist.md")
        assert result == ""
