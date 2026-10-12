"""Plain-English rule wording and count consistency for the browser UI."""

from __future__ import annotations

import csv
from pathlib import Path
from types import SimpleNamespace

from src.agent.state import AgentState, RuleResult
from src.ui.events import RunHub
from src.ui.rule_summary import (
    FIELD_LABELS,
    RULE_LABELS,
    RuleCatalog,
    field_label,
    humanize,
    summarize_compliance,
    summarize_run,
    summarize_visual,
)
from src.ui.web_presenter import WebPresenter

CONFIG = Path(__file__).resolve().parent.parent / "config"
RULES_CSV = CONFIG / "csv" / "compliance_rules.csv"


def test_every_field_name_in_the_configs_has_an_english_label():
    # The configs' own field_label column is Spanish; a new field must get a label here too.
    names = set()
    for path in (CONFIG / "csv" / "extraction_fields.csv", CONFIG / "csv_demo" / "extraction_fields.csv"):
        with path.open(encoding="utf-8", newline="") as f:
            names |= {row["field_name"] for row in csv.DictReader(f)}
    assert names - set(FIELD_LABELS) == set()
    assert field_label("vendor_name") == "Supplier"
    assert field_label("brand_new_field") == "Brand new field"


def _rule(rule_id, rule_name, check_type="required", severity="warning"):
    return SimpleNamespace(rule_id=rule_id, rule_name=rule_name, check_type=check_type, severity=severity)


CATALOG = RuleCatalog(
    [
        _rule("R_VIA_004", "destino_requerido"),
        _rule("R_VIA_007", "calculo_dietas", "cross_field"),
        _rule("R_VIA_001", "fecha_requerida", severity="error"),
        _rule("R_VIA_016", "payment_method_stated", "visual_check"),
        _rule("R_VIA_017", "proof_of_payment_attached", "visual_check"),
        _rule("R_NEW_001", "some_brand_new_rule"),
    ]
)


def test_every_rule_name_in_the_config_has_an_english_label():
    # Guards future edits: a newly added rule must get a label or it shows up as raw snake_case.
    with RULES_CSV.open(encoding="utf-8", newline="") as f:
        names = {row["rule_name"] for row in csv.DictReader(f)}
    assert names - set(RULE_LABELS) == set()


def test_labels_are_readable_not_identifiers():
    assert all("_" not in label for label in RULE_LABELS.values())


def test_label_uses_english_name_and_falls_back_gracefully():
    assert CATALOG.label("R_VIA_004") == "Trip destination stated"
    assert CATALOG.label("R_NEW_001") == "Some brand new rule"   # unknown name -> humanized
    assert CATALOG.label("R_UNKNOWN") == "R_UNKNOWN"             # unknown id -> shown as is


def test_humanize_only_touches_identifiers():
    assert humanize("compliance_passed") == "Compliance passed"
    assert humanize("Already a sentence.") == "Already a sentence."
    assert humanize("") == ""


def test_compliance_headline_counts_match_listed_rows_including_skipped():
    result = {
        "passed": 6,
        "failed_errors": [],
        "failed_warnings": [{"rule_id": "R_VIA_004", "message": "No se ha identificado el destino del viaje."}],
        "skipped_checks": [
            {"rule_id": "R_VIA_007", "severity": "warning",
             "reason": "Cross-field check skipped (missing values): per_diem_days"},
            {"rule_id": "R_VIA_001", "severity": "error", "reason": "Value missing/null — skipping regex check"},
        ],
        "visual_checks_pending": ["R_VIA_016", "R_VIA_017"],
    }
    primary, rows, _ = summarize_compliance(result, CATALOG)
    assert primary == (
        "6 passed · 0 blocking errors · 1 warning · 2 couldn't be checked · 2 visual checks still to run"
    )
    # Every counted rule appears as exactly one row, so the number and the list can't disagree.
    assert [r["outcome"] for r in rows] == ["warning", "not_checked", "not_checked"]
    assert rows[0]["label"] == "Trip destination stated"
    assert rows[0]["text"] == "Not found in the document"  # not the Spanish config message
    assert "Missing values: per_diem_days" in rows[1]["text"]
    assert "Does not block approval" in rows[1]["text"]
    assert "Blocks approval until checked" in rows[2]["text"]


def test_compliance_rows_never_expose_python_dicts():
    result = {"passed": 1, "failed_warnings": [{"rule_id": "R_VIA_004", "message": "x"}]}
    _, rows, _ = summarize_compliance(result, CATALOG)
    for row in rows:
        assert "{" not in row["label"] + row["text"] and "rule_id" not in row["label"] + row["text"]


def test_visual_headline_counts_failures_and_lists_each_in_plain_words():
    result = {
        "success": True,
        "visual_rules_checked": 5,
        "passed": 3,
        "failed_errors": [],
        "failed_warnings": [
            {"rule_id": "R_VIA_016", "message": "No payment method is visible on any page."},
            {"rule_id": "R_VIA_017", "message": "No receipt attached."},
        ],
        "backfilled_fields": ["payment_method"],
    }
    primary, rows, details = summarize_visual(result, CATALOG)
    assert primary == "5 visual rules checked · 3 passed · 0 blocking errors · 2 warnings"
    assert [r["label"] for r in rows] == ["Payment method stated", "Proof of payment attached"]
    assert rows[0]["text"] == "No payment method is visible on any page."  # the model's own observation
    assert details == [{"text": "Also filled in missing fields: payment_method", "level": "info"}]


def test_visual_headline_reports_inconclusive_rules():
    result = {"success": True, "visual_rules_checked": 4, "passed": 2, "failed_errors": [], "failed_warnings": []}
    primary, _, _ = summarize_visual(result, CATALOG)
    assert primary.endswith("2 inconclusive")


def test_final_summary_uses_same_buckets_and_counts_skipped_rules():
    results = [
        RuleResult(rule_id="R_VIA_001", rule_name="x", field_id="f", status="passed", severity="error", message="OK"),
        RuleResult(rule_id="R_VIA_004", rule_name="x", field_id="f", status="failed", severity="warning", message="m"),
        RuleResult(rule_id="R_VIA_007", rule_name="x", field_id="f", status="skipped", severity="warning",
                   message="Cross-field check skipped (missing values): per_diem_days"),
    ]
    summary = summarize_run(results, CATALOG)
    assert summary["passed"] == 1
    assert [r["label"] for r in summary["warnings"]] == ["Trip destination stated"]
    assert [r["label"] for r in summary["not_checked"]] == ["Allowance total matches days × daily rate"]


def test_step_and_final_summary_agree_for_the_same_run():
    """The bug this guards: step said '0 warnings' while the same run listed a warning below."""
    hub = RunHub("r1", "a.pdf")
    p = WebPresenter()
    p.set_rule_catalog(CATALOG)
    p.attach(hub)
    p.tool_start(1, "check_compliance", "", {})
    p.tool_result(
        "check_compliance",
        {
            "passed": 1,
            "failed_errors": [],
            "failed_warnings": [{"rule_id": "R_VIA_004", "message": "x"}],
            "skipped_checks": [{"rule_id": "R_VIA_007", "severity": "warning", "reason": "Value missing/null"}],
            "visual_checks_pending": [],
        },
        10,
    )
    step = [e for e in hub.events_after(0) if e["type"] == "tool_result"][0]
    assert "1 warning" in step["primary"] and "1 couldn't be checked" in step["primary"]

    state = AgentState(pdf_path="a.pdf", output_dir="out")
    state.rule_results = [
        RuleResult(rule_id="R_VIA_001", rule_name="x", field_id="f", status="passed", severity="error", message="OK"),
        RuleResult(rule_id="R_VIA_004", rule_name="x", field_id="f", status="failed", severity="warning", message="x"),
        RuleResult(rule_id="R_VIA_007", rule_name="x", field_id="f", status="skipped", severity="warning",
                   message="Value missing/null — skipping range check"),
    ]
    p.run_complete(state, paths={})
    rules = hub.events_after(0)[-1]["rules"]
    assert (len(rules["warnings"]), len(rules["not_checked"])) == (1, 1)
