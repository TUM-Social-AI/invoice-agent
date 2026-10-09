"""
Config placeholders in compliance rules ({project.*} / {compliance.*}), currency-converted limits
(cash limit, per-diem range) and non-blocking skips. Offline.
"""

from datetime import date

from PIL import Image

import src.tools.tool_wrappers as tool_wrappers
from src.agent.agent import build_tool_registry
from src.agent.state import AgentState, FieldResult
from src.config.loader import load_config, resolve_rule_text, unresolved_rule_params
from src.tools.compliance_eval import NOT_APPLICABLE_NOTE, NOT_CONFIGURED_NOTE
from src.tools.tools import check_compliance

COMPLIANCE = {
    "currency_per_eur": {"EUR": 1, "XAF": 655.957, "CFA": 655.957},
    "cash_limit_eur": 2500,
    "subsistence_abroad_max_eur_per_day": 95,
    "three_quotes_supplies_eur": 15000,
}
PROJECT = {"start_date": date(2025, 1, 1), "end_date": "2025-12-31"}


def _store(project=None, compliance=None):
    store = load_config("config/csv")
    store.set_rule_params({"project": project or {}, "compliance": compliance or {}})
    return store


def _state(fields: dict, store, invoice_type_id: str) -> AgentState:
    ids = {f.field_name: f.field_id for f in store.get_fields(invoice_type_id)}
    state = AgentState(pdf_path="test.pdf", invoice_type_id=invoice_type_id, output_dir="/tmp/test_output")
    for name, value in fields.items():
        state.extracted_fields[name] = FieldResult(
            field_id=ids.get(name, name), field_name=name, extracted_value=value,
            confidence=0.95, source_page=1, source_region="header",
        )
    return state


def _rule(store, invoice_type_id, rule_name):
    return next(r for r in store.get_rules(invoice_type_id) if r.rule_name == rule_name)


class TestResolveRuleText:
    def test_substitutes_scalars_dates_and_lists(self):
        params = {"project": {"start_date": date(2025, 1, 1), "names": ["Org A", "Org B"]}, "compliance": {"x": 0.21}}
        text, missing = resolve_rule_text("{project.start_date} {project.names} {compliance.x}", params)
        assert text == '2025-01-01 "Org A" or "Org B" 0.21'
        assert missing == []

    def test_unset_value_is_reported_and_left_in_place(self):
        text, missing = resolve_rule_text("from {project.start_date} to {project.end_date}", {"project": {"start_date": ""}})
        assert text == "from {project.start_date} to {project.end_date}"
        assert missing == ["project.start_date", "project.end_date"]

    def test_fallback_used_when_unset(self):
        text, missing = resolve_rule_text("number {project.file_number|any number}", {"project": {}})
        assert text == "number any number"
        assert missing == []

    def test_eur_format_lists_pegged_currencies_once(self):
        text, _ = resolve_rule_text("{eur:compliance.cash_limit_eur}", {"compliance": COMPLIANCE})
        assert text == "2500 EUR (= 1,639,892 XAF/CFA)"  # 1,639,892.5 rounded half to even

    def test_plain_braces_are_not_placeholders(self):
        text, missing = resolve_rule_text("a {b} {c.} {}", {})
        assert text == "a {b} {c.} {}"
        assert missing == []


class TestStoreResolution:
    def test_get_rules_resolves_configured_placeholders(self):
        store = _store(PROJECT, COMPLIANCE)
        rule = _rule(store, "EQUIPOS", "project_execution_within_period")
        assert "2025-01-01" in rule.check_value and "2025-12-31" in rule.check_value
        assert unresolved_rule_params(rule) == []

    def test_get_rules_keeps_unset_placeholders(self):
        rule = _rule(_store(), "EQUIPOS", "project_execution_within_period")
        assert unresolved_rule_params(rule) == ["project.start_date", "project.end_date", "project.start_date"]


class TestNotConfigured:
    def test_unset_period_rule_is_skipped_not_failed_and_not_blocking(self):
        store = _store(compliance=COMPLIANCE)
        rule = _rule(store, "EQUIPOS", "project_execution_within_period")
        state = _state({}, store, "EQUIPOS")
        result = check_compliance(state, [rule], store=store)
        rr = state.rule_results[0]
        assert rr.status == "skipped" and rr.agent_notes == NOT_CONFIGURED_NOTE
        assert "project.start_date" in rr.message
        assert result["visual_checks_pending"] == []
        assert result["skipped_checks"] == []
        assert result["not_configured_checks"][0]["rule_id"] == rule.rule_id
        assert result["all_errors_resolved"] is True

    def test_configured_period_rule_goes_to_visual(self):
        store = _store(PROJECT, COMPLIANCE)
        rule = _rule(store, "EQUIPOS", "project_execution_within_period")
        result = check_compliance(_state({}, store, "EQUIPOS"), [rule], store=store)
        assert result["visual_checks_pending"] == [rule.rule_id]


class TestCashLimit:
    def _run(self, fields, compliance=COMPLIANCE):
        store = _store(compliance=compliance)
        rule = _rule(store, "EQUIPOS", "cash_payment_below_limit")
        state = _state(fields, store, "EQUIPOS")
        return check_compliance(state, [rule], store=store), state.rule_results[0]

    def test_cash_above_limit_in_xaf_fails(self):
        # 2,500 EUR = 1,639,892.5 XAF
        result, rr = self._run({"payment_method": "cash", "total_amount": "1.700.000", "currency": "XAF"})
        assert rr.status == "failed"
        assert result["failed_errors"][0]["rule_id"] == rr.rule_id

    def test_cash_below_limit_in_xaf_passes(self):
        _, rr = self._run({"payment_method": "cash", "total_amount": "1 600 000", "currency": "XAF"})
        assert rr.status == "passed"

    def test_cash_exactly_at_limit_fails(self):
        # The regulation allows cash only for amounts below the limit.
        _, rr = self._run({"payment_method": "cash", "total_amount": "2500", "currency": "EUR"})
        assert rr.status == "failed"

    def test_non_cash_payment_is_not_applicable_and_not_blocking(self):
        result, rr = self._run({"payment_method": "bank_transfer", "total_amount": "9000000", "currency": "XAF"})
        assert rr.status == "skipped" and rr.agent_notes == NOT_APPLICABLE_NOTE
        assert result["skipped_checks"] == []
        assert result["all_errors_resolved"] is True

    def test_unknown_currency_rate_is_skipped_with_reason(self):
        _, rr = self._run({"payment_method": "cash", "total_amount": "100", "currency": "USD"})
        assert rr.status == "skipped"
        assert "USD" in rr.message

    def test_unset_limit_is_not_configured(self):
        _, rr = self._run({"payment_method": "cash", "total_amount": "100", "currency": "EUR"}, compliance={})
        assert rr.status == "skipped" and rr.agent_notes == NOT_CONFIGURED_NOTE


def test_normalize_numeric_dot_thousands():
    from src.tools.compliance_eval import _normalize_numeric

    assert _normalize_numeric("1.700.000") == 1700000.0
    assert _normalize_numeric("1.234.56") == 1234.56


class TestPerDiemRangeInEur:
    def test_xaf_per_diem_above_cap_fails(self):
        store = _store(compliance=COMPLIANCE)
        rule = next(r for r in store.get_rules("VIAJES") if r.rule_id == "R_VIA_006")
        state = _state({"per_diem_rate": "70 000", "currency": "XAF"}, store, "VIAJES")  # ~106.7 EUR
        check_compliance(state, [rule], store=store)
        assert state.rule_results[0].status == "failed"

    def test_xaf_per_diem_below_cap_passes(self):
        store = _store(compliance=COMPLIANCE)
        rule = next(r for r in store.get_rules("VIAJES") if r.rule_id == "R_VIA_006")
        state = _state({"per_diem_rate": "50 000", "currency": "XAF"}, store, "VIAJES")  # ~76.2 EUR
        check_compliance(state, [rule], store=store)
        assert state.rule_results[0].status == "passed"


class TestVatCrossFieldWithoutVat:
    def test_ht_invoice_without_vat_does_not_fail(self):
        store = _store(compliance=COMPLIANCE)
        rule = next(r for r in store.get_rules("EQUIPOS") if r.rule_id == "R_EQ_006")
        state = _state({"net_amount": "425000", "total_amount": "425000"}, store, "EQUIPOS")
        result = check_compliance(state, [rule], store=store)
        assert state.rule_results[0].status == "skipped"
        assert result["failed_warnings"] == [] and result["all_errors_resolved"] is True


class TestToolWrappers:
    def _tools(self, store):
        config = {"ollama": {"base_url": "http://localhost:11434", "vision_model": "x", "reasoning_model": "y"},
                  "ocr": {"langs": ["fr"]}, "agent": {"active_rule_groups": ["general", "aexcid"]}}
        return build_tool_registry(config=config, store=store, surya_models=None)

    def test_finish_not_blocked_by_not_configured_or_not_applicable(self):
        store = _store(compliance=COMPLIANCE)  # no project dates
        tools = self._tools(store)
        state = _state(
            {"payment_method": "bank_transfer", "total_amount": "1000", "currency": "EUR"}, store, "EQUIPOS"
        )
        rules = [_rule(store, "EQUIPOS", n) for n in ("project_execution_within_period", "cash_payment_below_limit")]
        check_compliance(state, rules, store=store)
        res = tools["finish"](state, reason="done", all_errors_resolved=True)
        assert res["finished"] is True
        assert res["evidence_gate"]["error_skipped"] is False
        assert res["evidence_gate"]["unresolved_error_evidence"] is False

    def test_visual_tool_never_receives_not_configured_rules(self, monkeypatch, tmp_path):
        store = _store(compliance=COMPLIANCE)
        tools = self._tools(store)
        page = tmp_path / "p1.png"
        Image.new("RGB", (10, 10)).save(page)
        state = _state({}, store, "EQUIPOS")
        state.page_image_paths = [str(page)]
        state.page_count = 1
        sent = {}

        def fake_visual(state, image_path, page_num, rules, *a, **kw):
            sent["rules"] = [r.rule_id for r in rules]
            return {"success": True}

        monkeypatch.setattr(tool_wrappers, "check_compliance_visual", fake_visual)
        tools["check_compliance_visual"](state, page_num=1)
        period = _rule(store, "EQUIPOS", "project_execution_within_period").rule_id
        assert sent["rules"] and period not in sent["rules"]
        assert _rule(store, "EQUIPOS", "aexcid_stamp_complete").rule_id in sent["rules"]
