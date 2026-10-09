import csv
from pathlib import Path

import yaml

from src.agent.state import AgentState, FieldResult, RuleResult
from src.config.loader import ComplianceRule, load_config
from src.learning.evaluator import (
    evaluate,
    format_diff_for_agent,
    load_compliance_review,
    load_ground_truth,
)

COMPLIANCE_FIELDS = ["Id", "Source file", "overall_compliant", "issue_code", "level", "note"]


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)


def _rule(rule_id: str, issue_code: str = "") -> ComplianceRule:
    return ComplianceRule(
        rule_id=rule_id,
        invoice_type_id="CONSUMIBLES",
        rule_name=rule_id.lower(),
        field_id="",
        check_type="visual_check",
        check_value="",
        severity="error",
        agent_hint="",
        error_message="",
        page_region="body",
        issue_code=issue_code,
    )


class _Store:
    """Minimal stand-in for ConfigStore: rules with issue codes, no field schema."""

    def __init__(self, rules: list[ComplianceRule]):
        self.compliance_rules = {"CONSUMIBLES": rules}

    def get_fields(self, invoice_type_id):
        return []


def _result(rule_id: str, status: str, severity: str = "error") -> RuleResult:
    return RuleResult(
        rule_id=rule_id,
        rule_name=rule_id.lower(),
        field_id="",
        status=status,
        severity=severity,
        message="",
    )


def _state(rule_results: list[RuleResult], fields: dict | None = None) -> AgentState:
    return AgentState(
        pdf_path="/tmp/x.pdf",
        output_dir="/tmp/out",
        invoice_type_id="CONSUMIBLES",
        rule_results=rule_results,
        extracted_fields={
            name: FieldResult(
                field_id=name,
                field_name=name,
                extracted_value=value,
                confidence=0.9,
                source_page=1,
                source_region="h",
            )
            for name, value in (fields or {}).items()
        },
    )


def test_compliance_loader_reads_issue_code_and_tolerates_missing_column(tmp_path: Path):
    store = load_config("config/csv")
    # Rules without the column still load; issue_code defaults to empty.
    assert all(isinstance(r.issue_code, str) for rules in store.compliance_rules.values() for r in rules)
    assert _rule("R_X", "aexcid_stamp").issue_code == "aexcid_stamp"


def test_load_compliance_review_groups_rows_per_pdf(tmp_path: Path):
    csv_path = tmp_path / "compliance.csv"
    _write_csv(csv_path, COMPLIANCE_FIELDS, [
        {"Id": "A1", "Source file": "A.4.c.- Consumibles-A1-25.pdf", "overall_compliant": "No",
         "issue_code": "aexcid_stamp", "level": "fail", "note": "stamp"},
        {"Id": "A1", "Source file": "A.4.c.- Consumibles-A1-25.pdf", "overall_compliant": "no",
         "issue_code": "tax_treatment", "level": "Verify", "note": "HT"},
        {"Id": "A2", "Source file": "A.4.c.- Consumibles-A2-25.pdf", "overall_compliant": "",
         "issue_code": "payment_proof", "level": "fail", "note": "other pdf"},
    ])
    config = {"agent": {"compliance_ground_truth_csv_path": str(csv_path)}}

    review = load_compliance_review(str(tmp_path / "A.4.c.- Consumibles-A1-25.pdf"), config=config)
    assert review["overall_compliant"] is False
    assert [(i["issue_code"], i["level"]) for i in review["issues"]] == [
        ("aexcid_stamp", "fail"),
        ("tax_treatment", "verify"),
    ]

    # Blank verdict stays unknown, not "yes".
    other = load_compliance_review(str(tmp_path / "A.4.c.- Consumibles-A2-25.pdf"), config=config)
    assert other["overall_compliant"] is None
    assert load_compliance_review(str(tmp_path / "unrelated.pdf"), config=config) is None


def test_load_ground_truth_attaches_review_without_field_row(tmp_path: Path):
    csv_path = tmp_path / "compliance.csv"
    _write_csv(csv_path, COMPLIANCE_FIELDS, [
        {"Id": "A1", "Source file": "doc.pdf", "overall_compliant": "no",
         "issue_code": "", "level": "", "note": "no reasons given"},
    ])
    config = {"agent": {"compliance_ground_truth_csv_path": str(csv_path)}}
    truth = load_ground_truth(str(tmp_path / "doc.pdf"), config=config)
    assert truth == {"fields": {}, "compliance_review": {"overall_compliant": False, "issues": []}}


def test_evaluate_scores_reviewer_issues():
    store = _Store([
        _rule("R_STAMP", "aexcid_stamp"),
        _rule("R_PAY", "payment_proof"),
        _rule("R_TAX", "tax_treatment"),
        _rule("R_ARITH", "invoice_arithmetic"),
        _rule("R_NOCODE"),
    ])
    review = {
        "overall_compliant": False,
        "issues": [
            {"id": "A1", "issue_code": "aexcid_stamp", "level": "fail", "note": ""},
            {"id": "A1", "issue_code": "payment_proof", "level": "fail", "note": ""},
            {"id": "A1", "issue_code": "tax_treatment", "level": "verify", "note": ""},
            {"id": "A1", "issue_code": "budget_line", "level": "verify", "note": ""},
            {"id": "A1", "issue_code": "external_documents", "level": "", "note": "financial report"},
        ],
    }
    state = _state([
        _result("R_STAMP", "failed"),
        _result("R_PAY", "skipped"),
        _result("R_TAX", "failed", severity="warning"),
        _result("R_ARITH", "failed", severity="warning"),
        _result("R_NOCODE", "failed", severity="warning"),
    ])

    diff = evaluate(state, {"fields": {}, "compliance_review": review}, store=store)
    s = diff["score"]
    assert (s["issues_fail_caught"], s["issues_fail_total"], s["issue_fail_recall"]) == (1, 2, 0.5)
    assert (s["issues_verify_caught"], s["issues_verify_total"], s["issue_verify_recall"]) == (1, 2, 0.5)

    rv = diff["compliance_review"]
    by_code = {i["issue_code"]: i for i in rv["issues"]}
    # Skipped rules never count as caught but are reported.
    assert by_code["payment_proof"]["caught"] is False
    assert by_code["payment_proof"]["skipped_rule_ids"] == ["R_PAY"]
    # Warning-severity failures count as caught.
    assert by_code["tax_treatment"]["rule_ids"] == ["R_TAX"]
    # external_documents is recorded but never scored.
    assert [i["issue_code"] for i in rv["unscored_issues"]] == ["external_documents"]
    assert "external_documents" not in by_code
    # Raised codes the reviewer did not mention are reported, not scored.
    assert rv["false_alarms"] == {"invoice_arithmetic": ["R_ARITH"]}
    assert s["issue_false_alarms"] == 1
    assert rv["failed_rules_without_issue_code"] == ["R_NOCODE"]
    # R_STAMP is an error-severity failure, so the agent says non-compliant like the reviewer.
    assert s["overall_compliant_match"] is True

    text = format_diff_for_agent(diff)
    assert "missed [fail] payment_proof" in text
    assert "not scored: external_documents" in text


def test_evaluate_overall_verdict_disagrees_when_only_warnings_fail():
    store = _Store([_rule("R_TAX", "tax_treatment")])
    state = _state([_result("R_TAX", "failed", severity="warning")])
    review = {"overall_compliant": False, "issues": []}
    s = evaluate(state, {"fields": {}, "compliance_review": review}, store=store)["score"]
    assert s["overall_compliant_match"] is False
    assert s["issue_fail_recall"] is None


def test_evaluate_currency_aliases_are_config_driven():
    store = load_config("config/csv")
    state = _state([], fields={"currency": "FCFA"})
    truth = {"fields": {"currency": "XAF"}}

    plain = evaluate(state, truth, store=store)
    assert plain["field_results"]["currency"]["match"] is False

    config = {"agent": {"ground_truth_value_aliases": {"currency": {"XAF": ["CFA", "FCFA", "F CFA"]}}}}
    aliased = evaluate(state, truth, store=store, config=config)
    assert aliased["field_results"]["currency"]["match"] is True

    state_spaced = _state([], fields={"currency": "f  cfa"})
    assert evaluate(state_spaced, truth, store=store, config=config)["field_results"]["currency"]["match"] is True


def test_multi_invoice_pdf_skips_field_scoring(tmp_path: Path):
    csv_path = tmp_path / "gt.csv"
    _write_csv(csv_path, ["Id", "Source file", "Supplier", "Invoice Type"], [
        {"Id": "U1-01", "Source file": "A.7. Serv-U1-25.pdf", "Supplier": "First", "Invoice Type": "CONSUMIBLES"},
        {"Id": "U1-02", "Source file": "A.7. Serv-U1-25.pdf", "Supplier": "Second", "Invoice Type": "CONSUMIBLES"},
        {"Id": "U2", "Source file": "A.7. Serv-U2-25.pdf", "Supplier": "Solo", "Invoice Type": "CONSUMIBLES"},
    ])
    config = {
        "agent": {
            "ground_truth_csv_path": str(csv_path),
            "ground_truth_invoice_type_column": "Invoice Type",
            "ground_truth_column_map": {"Supplier": "vendor_name"},
        }
    }

    truth = load_ground_truth(str(tmp_path / "A.7. Serv-U1-25.pdf"), config=config)
    assert truth["fields"] == {}
    assert truth["multi_invoice"]["row_ids"] == ["U1-01", "U1-02"]
    assert truth["invoice_type_id"] == "CONSUMIBLES"

    single = load_ground_truth(str(tmp_path / "A.7. Serv-U2-25.pdf"), config=config)
    assert "multi_invoice" not in single
    assert single["fields"]["vendor_name"] == "Solo"

    store = load_config("config/csv")
    diff = evaluate(_state([], fields={"vendor_name": "First"}), truth, store=store)
    assert diff["field_scoring_skipped"]
    assert diff["field_results"] == {}
    assert diff["score"]["fields_total"] == 0
    assert diff["score"]["type_correct"] is True
    assert "skipped" in format_diff_for_agent(diff)


def test_column_map_candidates_score_first_field_in_schema(tmp_path: Path):
    csv_path = tmp_path / "gt.csv"
    _write_csv(csv_path, ["Source file", "Client Name"], [{"Source file": "doc.pdf", "Client Name": "JRS"}])
    config = {
        "agent": {
            "ground_truth_csv_path": str(csv_path),
            "ground_truth_column_map": {"Client Name": ["not_a_field", "beneficiary", "vendor_name"]},
        }
    }
    truth = load_ground_truth(str(tmp_path / "doc.pdf"), config=config)
    store = load_config("config/csv")
    state = _state([], fields={"beneficiary": "JRS"})
    state.invoice_type_id = "VIAJES"  # has beneficiary and vendor_name
    diff = evaluate(state, truth, store=store)
    assert diff["field_results"]["beneficiary"]["match"] is True
    assert "vendor_name" not in diff["field_results"]  # later candidate dropped
    assert diff["score"]["fields_total"] == 1


def test_project_ground_truth_files_are_consistent():
    """The shipped CSVs resolve per PDF: U0205 has its own file, U0283 is multi-invoice."""
    config = yaml.safe_load(Path("config/config.yaml").read_text(encoding="utf-8"))

    u0205 = load_ground_truth("A.8.- Fonctionnement-U0205-25.pdf", config=config)
    assert u0205["fields"]["invoice_number"] == "0007915"
    assert u0205["compliance_review"] == {"overall_compliant": False, "issues": []}
    u0357 = load_ground_truth("A.8.- Fonctionnement-U0357-25.pdf", config=config)
    assert u0357["fields"]["invoice_number"] == "0012008"

    u0283 = load_ground_truth("A.7. Serv técn y prof-U0283-25.pdf", config=config)
    assert u0283["multi_invoice"]["row_ids"] == ["U0283-01", "U0283-02"]
    assert u0283["invoice_type_id"] == "SERV_TECNICOS"
    assert {i["issue_code"] for i in u0283["compliance_review"]["issues"]} >= {
        "unrelated_or_multiple_documents", "invoice_arithmetic",
    }

    with open(config["agent"]["ground_truth_csv_path"], newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    # Chad corpus in XAF, except the UNHAS flights (A3223) invoiced in USD.
    assert {r["Id"]: r["Currency"] for r in rows if r["Currency"] != "XAF"} == {"A3223": "USD"}
    assert all(r["Invoice Type"] for r in rows)

    taxonomy = {
        "aexcid_stamp", "payment_proof", "tax_treatment", "client_identification",
        "supplier_identification", "invoice_arithmetic", "document_consistency",
        "unrelated_or_multiple_documents", "execution_period", "description_insufficient",
        "budget_line", "volunteer_documentation", "external_documents",
    }
    with open(config["agent"]["compliance_ground_truth_csv_path"], newline="", encoding="utf-8") as f:
        issues = list(csv.DictReader(f))
    for r in issues:
        assert not r["issue_code"] or r["issue_code"] in taxonomy, r
        if r["issue_code"] == "external_documents":
            assert r["level"] == ""
        elif r["issue_code"]:
            assert r["level"] in ("fail", "verify"), r
