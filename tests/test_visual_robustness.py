"""Visual compliance robustness: unevaluated rules are skipped, page selection, page cap."""

import base64
import csv
import shutil
from dataclasses import dataclass
from pathlib import Path

import pytest
import requests

from src.agent.state import AgentState, AgentStatus
from src.config.loader import ComplianceRule, load_config
from src.tools.compliance_eval import check_compliance
from src.tools.compliance_visual import (
    check_compliance_visual,
    rule_evidence_categories,
    select_evidence_pages,
)
from src.tools.tool_wrappers import make_finish

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO7+R0kAAAAASUVORK5CYII="
)


@dataclass
class FakeLLMResult:
    content_text: str
    content_json: dict | None
    raw: dict
    model: str
    provider: str


class FakeProvider:
    """Returns fixed verdicts; optionally fails any call carrying more than `max_images` images."""

    provider_name = "fake"

    def __init__(self, verdicts: dict, max_images: int | None = None):
        self.verdicts = verdicts
        self.max_images = max_images
        self.image_counts: list[int] = []

    def generate_json(self, **kwargs):
        n = len(kwargs.get("images_b64", []))
        self.image_counts.append(n)
        if self.max_images is not None and n > self.max_images:
            raise requests.HTTPError(response=requests.Response())
        return FakeLLMResult("", dict(self.verdicts), {}, "fake", "fake")


def _rule(rule_id, check_value, severity="error", rule_name="visual_rule", evidence_categories=""):
    return ComplianceRule(
        rule_id=rule_id,
        invoice_type_id="VIAJES",
        rule_name=rule_name,
        field_id="x",
        check_type="visual_check",
        check_value=check_value,
        severity=severity,
        agent_hint="",
        error_message="",
        page_region="body",
        enabled=True,
        evidence_categories=evidence_categories,
    )


def _state(tmp_path, categories: list[tuple[str, str]]) -> AgentState:
    state = AgentState(pdf_path="x.pdf", output_dir=str(tmp_path), invoice_type_id="VIAJES")
    paths = []
    for i in range(len(categories)):
        p = tmp_path / f"p{i + 1}.png"
        p.write_bytes(_PNG)
        paths.append(str(p))
    state.page_image_paths = paths
    state.page_inventory = [
        {"page": i + 1, "category": cat, "description": desc} for i, (cat, desc) in enumerate(categories)
    ]
    return state


def _run(state, rules, provider, page_num=1, max_pages=6):
    return check_compliance_visual(
        state=state,
        image_path=state.page_image_paths[page_num - 1],
        page_num=page_num,
        rules=rules,
        ollama_url="http://unused",
        model="fake",
        provider=provider,
        max_evidence_pages=max_pages,
    )


def _by_id(state):
    return {r.rule_id: r for r in state.rule_results}


def test_rule_omitted_by_model_is_skipped_not_failed(tmp_path):
    state = _state(tmp_path, [("INVOICE_HEADER", "invoice"), ("SIGNATURE_STAMP", "stamp")])
    rules = [_rule("R_A", "official stamp visible"), _rule("R_B", "official signature visible")]
    state.visual_checks_pending = ["R_A", "R_B"]
    provider = FakeProvider({"R_A": {"passes": True, "confidence": 0.9, "observation": "stamp seen"}})

    res = _run(state, rules, provider)

    results = _by_id(state)
    assert results["R_A"].status == "passed"
    assert results["R_B"].status == "skipped"
    assert "no verdict" in results["R_B"].message
    assert "R_B" not in state.failed_rules
    assert state.rule_state["R_B"] == "needs_review"
    assert state.visual_checks_pending == []
    assert res["failed_errors"] == []
    assert [s["rule_id"] for s in res["not_evaluated"]] == ["R_B"]


def test_verdict_without_passes_key_is_skipped(tmp_path):
    state = _state(tmp_path, [("INVOICE_HEADER", "invoice")])
    provider = FakeProvider({"R_A": {"confidence": 0.4, "observation": "unclear"}})

    _run(state, [_rule("R_A", "invoice number visible")], provider)

    assert _by_id(state)["R_A"].status == "skipped"


def test_single_anchor_fallback_skips_rules_whose_pages_were_not_sent(tmp_path):
    state = _state(
        tmp_path,
        [
            ("INVOICE_HEADER", "invoice header"),
            ("SUPPORTING_DOC", "bank statement"),
            ("SIGNATURE_STAMP", "project stamp"),
        ],
    )
    rules = [
        _rule("R_HEAD", "invoice number and date visible", evidence_categories="INVOICE_HEADER"),
        _rule("R_PAY", "proof of payment must be attached", rule_name="payment_proof"),
    ]
    verdict = {"passes": False, "confidence": 0.8, "observation": "not visible"}
    provider = FakeProvider({"R_HEAD": {**verdict, "passes": True}, "R_PAY": verdict}, max_images=1)

    res = _run(state, rules, provider, page_num=1)

    assert res["success"] is True
    assert res["evidence_pages"] == [1]
    assert res["attempt"] == "full:single_anchor"
    results = _by_id(state)
    assert results["R_HEAD"].status == "passed"
    assert results["R_PAY"].status == "skipped"
    assert "evidence pages not sent" in results["R_PAY"].message
    assert "SUPPORTING_DOC" in results["R_PAY"].message
    assert res["failed_errors"] == []


def test_evidence_categories_column_drives_page_selection(tmp_path):
    state = _state(
        tmp_path,
        [
            ("INVOICE_HEADER", "invoice"),
            ("LINE_ITEMS", "lines"),
            ("TOTALS", "totals"),
            ("SUPPORTING_DOC", "order form"),
        ],
    )
    # Wording says "stamp" (keyword fallback would pick SIGNATURE_STAMP), column says TOTALS.
    rule = _rule("R_X", "stamp near the total", evidence_categories="TOTALS")
    assert rule_evidence_categories(rule) == {"TOTALS"}
    provider = FakeProvider({"R_X": {"passes": True, "confidence": 0.9, "observation": "ok"}})

    res = _run(state, [rule], provider)

    assert res["evidence_pages"] == [1, 3]
    assert _by_id(state)["R_X"].status == "passed"


def test_evidence_categories_all_selects_every_page_within_cap():
    inv = {p: {"page": p, "category": c} for p, c in enumerate(
        ["INVOICE_HEADER", "LINE_ITEMS", "TOTALS", "SUPPORTING_DOC", "COVER_PAGE"], start=1)}
    selected, dropped = select_evidence_pages(1, inv, set(inv), [{"ALL"}], 8)
    assert selected == [1, 2, 3, 4, 5]
    assert dropped == []
    selected, dropped = select_evidence_pages(1, inv, set(inv), [{"ALL"}], 3)
    assert len(selected) == 3 and selected[0] == 1
    assert sorted(selected + dropped) == [1, 2, 3, 4, 5]


@pytest.mark.parametrize(
    "rule_name, check_value, expected",
    [
        ("stamp_aexcid", "Le document porte le cachet AEXCID", {"SIGNATURE_STAMP"}),
        ("arithmetic", "quantity x unit price equals the line total", {"LINE_ITEMS", "TOTALS"}),
        ("tax_regime", "TVA and HT amounts are consistent with the tax regime", {"LINE_ITEMS", "TOTALS"}),
        ("amount_in_words", "Le montant en lettres correspond au total", {"LINE_ITEMS", "TOTALS"}),
        ("paid_mark", "La facture est acquittée ou porte la mention payé", {"SUPPORTING_DOC", "TOTALS"}),
        ("receipt", "Un reçu de paiement est joint", {"SUPPORTING_DOC"}),
        ("unrelated_pages", "No unrelated pages are attached", {"ALL", "SUPPORTING_DOC"}),
        ("pro_forma", "A pro forma or purchase order is attached", {"SUPPORTING_DOC"}),
        ("supplier_identification", "Supplier name and tax id are identifiable", {"SUPPORTING_DOC"}),
        ("expense_description_sufficient", "The consumable goods purchased are described in enough detail",
         {"LINE_ITEMS"}),
        ("invoice_number", "Invoice number is visible", {"INVOICE_HEADER"}),
    ],
)
def test_keyword_fallback_covers_new_rule_wordings(rule_name, check_value, expected):
    cats = rule_evidence_categories(_rule("R", check_value, rule_name=rule_name))
    assert expected <= cats


def test_keyword_fallback_does_not_match_inside_words():
    # "ht" in "right"/"weight", "order" in "in order to", "page" alone.
    cats = rule_evidence_categories(_rule("R", "the right weight on the first page in order to read it"))
    assert cats == {"INVOICE_HEADER"}


def _twelve_page_inventory():
    cats = (
        [("INVOICE_HEADER", "invoice"), ("LINE_ITEMS", "lines"), ("TOTALS", "totals"),
         ("SIGNATURE_STAMP", "stamp")]
        + [("SUPPORTING_DOC", d) for d in (
            "order form", "pro forma", "delivery note", "quote", "funds request", "travel ticket", "hotel note")]
        + [("SUPPORTING_DOC", "bank statement showing the transfer")]
    )
    return cats


def test_page_cap_keeps_bank_statement_on_twelve_page_document(tmp_path):
    state = _state(tmp_path, _twelve_page_inventory())
    rules = [
        _rule("R_PAY", "proof of payment must be attached", rule_name="payment_proof"),
        _rule("R_STAMP", "project stamp is present"),
        _rule("R_SUM", "line totals add up to the invoice total"),
    ]
    verdict = {"passes": True, "confidence": 0.9, "observation": "ok"}
    provider = FakeProvider({r.rule_id: verdict for r in rules})

    res = _run(state, rules, provider, page_num=4, max_pages=6)

    sent = res["evidence_pages"]
    assert len(sent) == 6
    assert sent[0] == 4  # anchor kept first
    assert 12 in sent  # bank statement survives the cap
    assert {1, 2, 3} <= set(sent)  # every needed category is covered
    assert res["pages_dropped"] == sorted(set(range(1, 13)) - set(sent))
    assert all(_by_id(state)[r.rule_id].status == "passed" for r in rules)


def test_page_cap_selection_is_deterministic():
    inv = {i + 1: {"page": i + 1, "category": c, "description": d}
           for i, (c, d) in enumerate(_twelve_page_inventory())}
    needs = [{"SUPPORTING_DOC", "TOTALS", "SIGNATURE_STAMP"}]
    first = select_evidence_pages(4, inv, set(inv), needs, 5)
    for _ in range(3):
        assert select_evidence_pages(4, inv, set(inv), needs, 5) == first


def test_page_cap_payment_markers_in_page_facts_prioritise_page():
    inv = {1: {"category": "INVOICE_HEADER"}, 2: {"category": "SUPPORTING_DOC"},
           3: {"category": "SUPPORTING_DOC"}, 4: {"category": "SUPPORTING_DOC"}}
    facts = {4: {"entities": {"payment_markers": ["transfer"]}}}
    selected, dropped = select_evidence_pages(1, inv, set(inv), [{"SUPPORTING_DOC"}], 2, page_facts=facts)
    assert selected == [1, 4]
    assert dropped == [2, 3]


def test_skipped_visual_error_rule_does_not_loop_and_finishes_needs_review(tmp_path):
    state = _state(tmp_path, [("INVOICE_HEADER", "invoice"), ("SIGNATURE_STAMP", "stamp")])
    rules = [_rule("R_STAMP", "official stamp visible", severity="error")]

    first = check_compliance(state, rules)
    assert first["visual_checks_pending"] == ["R_STAMP"]

    _run(state, rules, FakeProvider({}))  # model omits the rule
    assert _by_id(state)["R_STAMP"].status == "skipped"
    assert state.visual_checks_pending == []

    # Re-running check_compliance must not re-queue the visual rule (that would loop forever).
    for _ in range(3):
        again = check_compliance(state, rules)
        assert again["visual_checks_pending"] == []
        assert state.visual_checks_pending == []
        assert [s["rule_id"] for s in again["visual_not_evaluated"]] == ["R_STAMP"]
        assert again["skipped_checks"] == []
        assert again["all_errors_resolved"] is False

    out = make_finish(None)(state, reason="done")
    assert out["finished"] is True
    assert out["status"] == AgentStatus.NEEDS_REVIEW.value
    assert out["error_failures"] == []
    assert out["evidence_gate"]["error_skipped"] is False
    assert out["evidence_gate"]["visual_not_evaluated"] == ["R_STAMP"]


def test_skipped_visual_rule_is_re_evaluated_on_a_later_successful_call(tmp_path):
    state = _state(tmp_path, [("INVOICE_HEADER", "invoice"), ("SIGNATURE_STAMP", "stamp")])
    rules = [_rule("R_STAMP", "official stamp visible")]
    _run(state, rules, FakeProvider({}))
    _run(state, rules, FakeProvider({"R_STAMP": {"passes": False, "confidence": 0.9, "observation": "none"}}))

    assert _by_id(state)["R_STAMP"].status == "failed"
    assert "visual_skip_reason" not in state.rule_evidence["R_STAMP"]
    assert check_compliance(state, rules)["visual_not_evaluated"] == []


def _copy_config(tmp_path: Path, evidence: dict[str, str]) -> Path:
    dst = tmp_path / "csv"
    shutil.copytree("config/csv", dst)
    path = dst / "compliance_rules.csv"
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)
    if "evidence_categories" not in fieldnames:
        fieldnames.append("evidence_categories")
    for row in rows:
        row["evidence_categories"] = evidence.get(row["rule_id"], "")
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return dst


def _first_enabled_rule_id() -> str:
    with open("config/csv/compliance_rules.csv", newline="", encoding="utf-8") as f:
        return next(r["rule_id"] for r in csv.DictReader(f) if r["enabled"].lower() == "true")


def test_loader_parses_optional_evidence_categories_column(tmp_path):
    rid = _first_enabled_rule_id()
    store = load_config(str(_copy_config(tmp_path, {rid: "supporting_doc; TOTALS"})))
    rules = {r.rule_id: r for rs in store.compliance_rules.values() for r in rs}
    assert rules[rid].evidence_categories == ["SUPPORTING_DOC", "TOTALS"]
    assert all(r.evidence_categories == [] for k, r in rules.items() if k != rid)


def test_loader_rejects_unknown_evidence_category(tmp_path):
    rid = _first_enabled_rule_id()
    with pytest.raises(ValueError):
        load_config(str(_copy_config(tmp_path, {rid: "BANK_STATEMENT"})))
