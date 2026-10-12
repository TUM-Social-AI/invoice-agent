"""trace.json: fields carry grounded evidence, findings carry status, evidence and expected markers."""

import json

import pytest

from src.agent.state import AgentState, AgentStatus, FieldResult, RuleResult
from src.config.loader import load_config
from src.trace.evidence import Evidence
from src.trace.export import build_trace
from src.trace.ocr_cache import _source_key


@pytest.fixture(scope="module")
def store():
    return load_config("config/csv")


def _state(tmp_path, store):
    st = AgentState(pdf_path=str(tmp_path / "inv.pdf"), output_dir=str(tmp_path), invoice_type_id="VIAJES")
    st.page_count = 1
    st.status = AgentStatus.NEEDS_REVIEW
    ocr_dir = tmp_path / "tmp" / "ocr"
    ocr_dir.mkdir(parents=True)
    (tmp_path / "inv.pdf").write_bytes(b"%PDF-1.4 test")
    (ocr_dir / "page_001.json").write_text(json.dumps({
        "page": 1, "image_path": "x.jpg", "width": 1000, "height": 1400,
        "rotation": 0, "source_key": _source_key(st),
        "lines": [
            {"text": "Fecha:", "confidence": 0.9, "bbox": [50, 100, 120, 130]},          # L0 empty date label
            {"text": "Importe total: 135,00", "confidence": 0.9, "bbox": [50, 900, 500, 930]},  # L1
        ],
    }))
    fields = {f.field_name: f for f in store.get_fields("VIAJES")}
    total = fields["total_amount"]
    st.extracted_fields["total_amount"] = FieldResult(
        field_id=total.field_id, field_name="total_amount", extracted_value=135.0, confidence=0.92,
        source_page=1, source_region="totals", evidence=[Evidence(page=1, line_ids=["L1"])],
    )
    return st


def test_fields_and_findings(tmp_path, store):
    st = _state(tmp_path, store)
    rules = {r.rule_id: r for r in store.get_rules("VIAJES")}
    date_rule = rules["R_VIA_001"]  # invoice date required, not extracted
    total_rule = rules["R_VIA_002"]
    visual = next(r for r in rules.values() if r.check_type == "visual_check")
    st.rule_results = [
        RuleResult(rule_id=date_rule.rule_id, rule_name=date_rule.rule_name, field_id=date_rule.field_id,
                   status="failed", severity="error", message="missing"),
        RuleResult(rule_id=total_rule.rule_id, rule_name=total_rule.rule_name, field_id=total_rule.field_id,
                   status="passed", severity="error", message="ok"),
        RuleResult(rule_id=visual.rule_id, rule_name=visual.rule_name, field_id="VISUAL",
                   status="failed", severity=visual.severity, message="No observation"),
    ]
    st.rule_evidence[visual.rule_id] = {"trace": {
        "page_num": 1, "pages_sent": [1], "line_ids": [], "evidence_kind": "",
        "verdict_missing": True, "observation": "No observation", "confidence": 0.5,
    }}

    t = build_trace(st, store, {"llm": {"provider": "openai"}, "openai": {"vision_model": "m"}})

    total = next(f for f in t["fields"] if f["field_name"] == "total_amount")
    assert total["evidence"]["quality"] == "confirmed"
    assert total["evidence"]["line_ids"] == ["L1"]

    by_rule = {f["rule_id"]: f for f in t["findings"]}
    date_f = by_rule["R_VIA_001"]
    assert date_f["status"] == "failed"
    assert date_f["expected"] and date_f["expected"]["page"] == 1, "marker on the empty Fecha label"
    assert by_rule["R_VIA_002"]["status"] == "passed"
    assert by_rule["R_VIA_002"]["evidence"][0]["line_ids"] == ["L1"]
    assert by_rule[visual.rule_id]["status"] == "not_evaluated", "a skipped verdict is not reported as absent"
    assert t["findings"][0]["rule_id"] == "R_VIA_001", "failed findings come first"
    assert t["findings"][0]["ref"] == "F1"


def test_stale_ocr_cache_is_ignored(tmp_path, store):
    st = _state(tmp_path, store)
    st.page_rotation[1] = 90  # page was turned after this OCR was cached
    from src.trace.ocr_cache import load_page_ocr_record
    assert load_page_ocr_record(st, 1) is None
