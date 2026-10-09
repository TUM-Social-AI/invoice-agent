"""Page document roles (inventory) and how extraction merges values across pages by role."""

from types import SimpleNamespace

import pytest
import yaml

from src.agent.state import AgentState
from src.config.loader import load_config
from src.models.tool_io_models import DOCUMENT_ROLES, InventoryItemModel, inventory_label
from src.prompts.llm_prompts import page_inventory_batch_prompt, page_inventory_prompt, page_role_note
from src.tools.page_inventory import _INVENTORY_BATCH_SCHEMA, inventory_pages, page_document_role
from src.tools.vision_llm import _build_extraction_response_schema, merge_extracted_fields

ROLE_PRIORITY = yaml.safe_load(open("config/config.yaml", encoding="utf-8"))["agent"]["document_role_priority"]


def _state(roles: dict[int, str]) -> AgentState:
    state = AgentState(pdf_path="/tmp/x.pdf", invoice_type_id="CONSUMIBLES", output_dir="/tmp/out")
    state.page_inventory = [
        {"page": p, "category": "INVOICE_HEADER", "document_role": r, "description": ""} for p, r in roles.items()
    ]
    return state


@pytest.fixture(scope="module")
def schema():
    return load_config("config/csv").build_extraction_schema("CONSUMIBLES")


def _merge(state, schema, page, values, conf=0.9):
    extraction = {}
    for k, v in values.items():
        extraction[k] = v
        extraction[f"{k}_confidence"] = conf
    return merge_extracted_fields(
        state, extraction, {k: schema[k] for k in values}, source_page=page, source_region="test",
        role_priority=ROLE_PRIORITY,
    )


# --- Inventory -------------------------------------------------------------------------------


def test_inventory_item_role_is_normalized():
    assert InventoryItemModel.model_validate({"category": "invoice_header", "document_role": " Funds_Request "}).document_role == "funds_request"
    assert InventoryItemModel.model_validate({"category": "TOTALS", "document_role": "invoice"}).document_role == ""
    assert InventoryItemModel.model_validate({"category": "TOTALS"}).document_role == ""


def test_inventory_label_and_lookup():
    state = _state({1: "funds_request", 2: ""})
    assert inventory_label(state.page_inventory[0]) == "INVOICE_HEADER, funds_request"
    assert inventory_label(state.page_inventory[1]) == "INVOICE_HEADER"
    assert page_document_role(state, 1) == "funds_request"
    assert page_document_role(state, "1") == "funds_request"
    assert page_document_role(state, 9) == ""
    assert page_document_role(state, None) == ""


def test_inventory_prompts_and_schema_list_every_role():
    for role in DOCUMENT_ROLES:
        assert role in page_inventory_prompt()
        assert role in page_inventory_batch_prompt(3)
    item = _INVENTORY_BATCH_SCHEMA["properties"]["pages"]["items"]
    assert item["properties"]["document_role"]["enum"] == list(DOCUMENT_ROLES)
    assert "document_role" in item["required"]


def test_inventory_pages_stores_roles(tmp_path):
    pages = []
    for i in range(2):
        p = tmp_path / f"p{i}.jpg"
        p.write_bytes(b"\xff\xd8\xff\xd9")
        pages.append(str(p))
    reply = {"pages": [
        {"category": "INVOICE_HEADER", "document_role": "funds_request", "description": "JRS Demande de fonds"},
        {"category": "INVOICE_HEADER", "document_role": "supplier_invoice", "description": "Facture ETS X"},
    ]}
    provider = SimpleNamespace(generate_json=lambda **kw: SimpleNamespace(content_json=reply, content_text=""))
    state = AgentState(pdf_path="/tmp/x.pdf", output_dir=str(tmp_path))
    state.compressed_page_paths = pages
    res = inventory_pages(state, "", "m", provider=provider, batch_size=0)
    assert res["success"]
    assert [e["document_role"] for e in state.page_inventory] == ["funds_request", "supplier_invoice"]
    # The layout category is untouched, so visual evidence selection by category is unchanged.
    assert [e["category"] for e in state.page_inventory] == ["INVOICE_HEADER", "INVOICE_HEADER"]
    assert state.page_facts[1]["document_role"] == "funds_request"


def test_page_role_note():
    assert "not the supplier's invoice" in page_role_note("funds_request")
    assert "Autorisé par" in page_role_note("funds_request")
    assert page_role_note("supplier_invoice") == ""
    assert page_role_note("") == ""


# --- Merge by role ---------------------------------------------------------------------------


def test_supplier_invoice_beats_more_confident_funds_request_for_vendor(schema):
    state = _state({1: "funds_request", 3: "supplier_invoice"})
    _merge(state, schema, 1, {"vendor_name": "JRS - TCHAD"}, conf=0.95)
    r = _merge(state, schema, 3, {"vendor_name": "ETS SERVICE ABOU HAMDAN"}, conf=0.8)
    assert state.extracted_fields["vendor_name"].extracted_value == "ETS SERVICE ABOU HAMDAN"
    assert r["updated"] == ["vendor_name"]
    # A later, more confident funds-request read does not take it back.
    r = _merge(state, schema, 1, {"vendor_name": "JRS"}, conf=0.99)
    assert state.extracted_fields["vendor_name"].extracted_value == "ETS SERVICE ABOU HAMDAN"
    assert r["already_have_better"] == ["vendor_name"]


def test_funds_request_number_beats_supplier_invoice_number(schema):
    state = _state({1: "funds_request", 3: "supplier_invoice", 6: "internal_other"})
    _merge(state, schema, 3, {"invoice_number": "006/09/2025"}, conf=0.95)
    _merge(state, schema, 6, {"invoice_number": "TCD01/0354/2025"}, conf=0.99)
    assert state.extracted_fields["invoice_number"].extracted_value == "006/09/2025"
    _merge(state, schema, 1, {"invoice_number": "0002880"}, conf=0.8)
    assert state.extracted_fields["invoice_number"].extracted_value == "0002880"


def test_low_confidence_higher_role_does_not_override(schema):
    state = _state({1: "funds_request", 3: "supplier_invoice"})
    _merge(state, schema, 1, {"total_amount": "425.000"}, conf=0.9)
    _merge(state, schema, 3, {"total_amount": "125 000"}, conf=0.3)
    assert state.extracted_fields["total_amount"].extracted_value == 425000.0


def test_payment_proof_wins_payment_method(schema):
    state = _state({1: "funds_request", 3: "payment_proof"})
    _merge(state, schema, 1, {"payment_method": "bank_transfer"}, conf=0.95)
    _merge(state, schema, 3, {"payment_method": "cheque"}, conf=0.7)
    assert state.extracted_fields["payment_method"].extracted_value == "cheque"


def test_fields_without_priority_keep_most_confident(schema):
    state = _state({1: "funds_request", 3: "supplier_invoice"})
    _merge(state, schema, 1, {"item_description": "Matériels et pause café"}, conf=0.9)
    _merge(state, schema, 3, {"item_description": "Mouchoir de table"}, conf=0.8)
    assert state.extracted_fields["item_description"].extracted_value == "Matériels et pause café"


def test_unknown_roles_fall_back_to_confidence(schema):
    state = _state({1: "", 2: ""})
    _merge(state, schema, 1, {"vendor_name": "A"}, conf=0.9)
    _merge(state, schema, 2, {"vendor_name": "B"}, conf=0.8)
    assert state.extracted_fields["vendor_name"].extracted_value == "A"


# --- Value checks at merge -------------------------------------------------------------------


def test_amounts_are_parsed_from_printed_text(schema):
    state = _state({1: "supplier_invoice"})
    _merge(state, schema, 1, {"total_amount": "50.000 R", "net_amount": "425 000 FCFA"})
    assert state.extracted_fields["total_amount"].extracted_value == 50000.0
    assert state.extracted_fields["net_amount"].extracted_value == 425000.0


def test_unreadable_amount_is_rejected_with_reason(schema):
    state = _state({1: "supplier_invoice"})
    r = _merge(state, schema, 1, {"total_amount": "cinquante mille"})
    assert state.extracted_fields["total_amount"].extracted_value is None
    assert "total_amount" in r["rejected"]
    assert "amount" in state.extracted_fields["total_amount"].review_reason


@pytest.mark.parametrize("fragment", ["31", "28", "07/2025", "juillet 2025"])
def test_partial_dates_are_rejected(schema, fragment):
    state = _state({1: "funds_request"})
    r = _merge(state, schema, 1, {"invoice_date": fragment})
    assert state.extracted_fields["invoice_date"].extracted_value is None
    assert "complete date" in r["rejected"]["invoice_date"]
    assert "complete date" in state.extracted_fields["invoice_date"].review_reason
    assert r["null_fields"] == ["invoice_date"]


def test_partial_date_does_not_erase_a_full_one(schema):
    state = _state({1: "funds_request", 2: "supplier_invoice"})
    _merge(state, schema, 1, {"invoice_date": "31 juillet 2025"})
    _merge(state, schema, 2, {"invoice_date": "07/2025"})
    assert state.extracted_fields["invoice_date"].extracted_value == "2025-07-31"


def test_full_dates_are_stored_as_iso(schema):
    state = _state({1: "funds_request"})
    _merge(state, schema, 1, {"invoice_date": "11/09/2025"})
    assert state.extracted_fields["invoice_date"].extracted_value == "2025-09-11"


def test_extraction_schema_requests_amounts_as_text(schema):
    for provider in ("openai", "ollama"):
        props = _build_extraction_response_schema(schema, provider)["properties"]
        assert props["total_amount"] == {"anyOf": [{"type": "string"}, {"type": "null"}]}
    props = _build_extraction_response_schema(schema, "gemini")["properties"]
    assert props["total_amount"]["type"] == "STRING"


# --- Uncovered role pages before compliance --------------------------------------------------


def test_uncovered_role_pages_are_extracted_once():
    from src.tools.tool_wrappers import _extract_uncovered_role_pages

    store = load_config("config/csv")
    state = _state({1: "funds_request", 2: "internal_other", 3: "supplier_invoice", 4: "payment_proof"})
    state.page_image_paths = ["p1", "p2", "p3", "p4"]
    state.record_action("extract_fields_vision", {"page_num": 3}, {"success": True}, "agent")
    calls = []

    def fake_extract(st, **kw):
        calls.append(kw)
        return {"success": True}

    cfg = {"auto_extract_page_roles": ["funds_request", "supplier_invoice", "payment_proof"],
           "document_role_priority": ROLE_PRIORITY}
    assert _extract_uncovered_role_pages(state, fake_extract, cfg, store) == [1, 4]
    assert [c["page_num"] for c in calls] == [1, 4]
    assert "invoice_number" in calls[0]["field_subset"]
    assert "item_description" not in calls[0]["field_subset"]  # not ranked by role

    # Recorded in the check result, so the next check_compliance does not extract them again.
    state.record_action("check_compliance", {}, {"auto_extracted_pages": [1, 4]}, "agent")
    calls.clear()
    assert _extract_uncovered_role_pages(state, fake_extract, cfg, store) == []
    assert calls == []


def test_uncovered_role_pages_disabled_by_empty_config():
    from src.tools.tool_wrappers import _extract_uncovered_role_pages

    state = _state({1: "funds_request"})
    state.page_image_paths = ["p1"]
    assert _extract_uncovered_role_pages(
        state, lambda *a, **k: {"success": True}, {"auto_extract_page_roles": []}, load_config("config/csv")
    ) == []
