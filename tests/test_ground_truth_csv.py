import csv
from pathlib import Path

from src.agent.state import AgentState, FieldResult
from src.config.loader import load_config
from src.learning.evaluator import (
    _compare_value,
    _ground_truth_match_key,
    _normalize_date_value,
    evaluate,
    ground_truth_csv_configured,
    load_ground_truth,
)


def test_ground_truth_match_key_multidot_without_pdf_suffix():
    # Pathlib stem would truncate "A.5.d.- ..." to "A.5.d" — must not.
    k = _ground_truth_match_key("A.5.d.- Personnel volontaire-U0279-25")
    assert k == "a5dpersonnelvolontaireu027925"
    assert _ground_truth_match_key("A.5.d.- Personnel volontaire-U0279-25.pdf") == k


def test_load_ground_truth_csv_row_distinguishes_u0223_vs_u0279(tmp_path: Path):
    """Source file values without .pdf must not all collapse to the same key."""
    csv_path = tmp_path / "gt.csv"
    fieldnames = ["Source file", "Invoice Num"]
    rows = [
        {"Source file": "A.5.d.- Personnel volontaire-U0223-25", "Invoice Num": "wrong"},
        {"Source file": "A.5.d.- Personnel volontaire-U0279-25", "Invoice Num": "6538"},
    ]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)

    config = {
        "agent": {
            "ground_truth_csv_path": str(csv_path),
            "ground_truth_source_column": "Source file",
            "ground_truth_column_map": {"Invoice Num": "invoice_number"},
        }
    }
    pdf = str(tmp_path / "A.5.d.- Personnel volontaire-U0279-25.pdf")
    truth = load_ground_truth(pdf, config=config)
    assert truth is not None
    assert truth["fields"]["invoice_number"] == "6538"


def test_load_ground_truth_from_csv_matches_pdf_stem(tmp_path: Path):
    # Minimal CSV with configurable column mapping.
    csv_path = tmp_path / "ground_truth.csv"
    fieldnames = ["Source file", "Invoice Num", "Invoice Date", "Total Amount", "Invoice Type"]

    rows = [
        {
            "Source file": "A.pdf",
            "Invoice Num": "6538",
            "Invoice Date": "28/08/2025",
            "Total Amount": "30000",
            "Invoice Type": "VIAJES",
        }
    ]

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    config = {
        "agent": {
            "ground_truth_csv_path": str(csv_path),
            "ground_truth_source_column": "Source file",
            "ground_truth_invoice_type_column": "Invoice Type",
            "ground_truth_column_map": {
                "Invoice Num": "invoice_number",
                "Invoice Date": "invoice_date",
                "Total Amount": "total_amount",
            },
        }
    }

    # No `<stem>_truth.json` exists; loader should fall back to CSV.
    pdf_path = str(tmp_path / "A.pdf")
    truth = load_ground_truth(pdf_path, config=config)

    assert truth is not None
    assert truth["invoice_type_id"] == "VIAJES"
    assert truth["fields"]["invoice_number"] == "6538"
    assert truth["fields"]["invoice_date"] == "28/08/2025"
    assert truth["fields"]["total_amount"] == "30000"


def test_load_ground_truth_from_csv_fuzzy_substring_match(tmp_path: Path):
    csv_path = tmp_path / "ground_truth.csv"
    fieldnames = ["Source file", "Invoice Num"]
    rows = [{"Source file": "invoice_ABC123_more.pdf", "Invoice Num": "999"}]

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    config = {
        "agent": {
            "ground_truth_csv_path": str(csv_path),
            "ground_truth_source_column": "Source file",
            "ground_truth_column_map": {"Invoice Num": "invoice_number"},
        }
    }

    pdf_path = str(tmp_path / "ABC123.pdf")
    truth = load_ground_truth(pdf_path, config=config)
    assert truth is not None
    assert truth["fields"]["invoice_number"] == "999"


def test_load_ground_truth_prefers_csv_per_invoice_type(tmp_path: Path):
    default_csv = tmp_path / "default.csv"
    viajes_csv = tmp_path / "viajes.csv"
    for path, num in ((default_csv, "1"), (viajes_csv, "2")):
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["Source file", "Invoice Num"])
            w.writeheader()
            w.writerow({"Source file": "doc.pdf", "Invoice Num": num})

    config = {
        "agent": {
            "ground_truth_csv_path": str(default_csv),
            "ground_truth_csv_by_invoice_type": {"VIAJES": str(viajes_csv)},
            "ground_truth_source_column": "Source file",
            "ground_truth_column_map": {"Invoice Num": "invoice_number"},
        }
    }
    pdf = str(tmp_path / "doc.pdf")
    assert load_ground_truth(pdf, config=config, invoice_type_id="CONSUMIBLES")["fields"]["invoice_number"] == "1"
    assert load_ground_truth(pdf, config=config, invoice_type_id="VIAJES")["fields"]["invoice_number"] == "2"


def test_normalize_date_iso_and_slashes():
    assert _normalize_date_value("2025-09-18") == "2025-09-18"
    assert _normalize_date_value("15/08/2025", "DMY") == "2025-08-15"
    assert _normalize_date_value("9/18/2025", "DMY") == "2025-09-18"  # b>12 → MDY parse


def test_normalize_date_ambiguous_slash_order():
    assert _normalize_date_value("1/2/2025", "DMY") == "2025-02-01"
    assert _normalize_date_value("1/2/2025", "MDY") == "2025-01-02"


def test_compare_pay_period_rejects_month_vs_year_numeric():
    r = _compare_value(8.0, 2025.0, "pay_period", date_parse="DMY")
    assert r["match"] is False
    assert "month" in r["note"].lower()


def test_compare_pay_period_same_calendar_year():
    r = _compare_value("Août 2025", "2025.0", "pay_period", date_parse="DMY")
    assert r["match"] is True


def test_compare_payment_method_rejects_date_fragment():
    r = _compare_value("11/1", "Paid by X", "payment_method", date_parse="DMY")
    assert r["match"] is False
    assert "date" in r["note"].lower()


def test_ground_truth_csv_configured():
    assert ground_truth_csv_configured({"agent": {"ground_truth_csv_path": "a.csv"}})
    assert ground_truth_csv_configured({"agent": {"ground_truth_csv_by_invoice_type": {"X": "y.csv"}}})
    assert not ground_truth_csv_configured({"agent": {}})


def test_evaluate_schema_filters_truth_fields_not_in_type():
    store = load_config("config/csv")
    state = AgentState(
        pdf_path="/tmp/x.pdf",
        output_dir="/tmp/out",
        invoice_type_id="CONSUMIBLES",
        extracted_fields={
            "vendor_name": FieldResult(
                field_id="c1",
                field_name="vendor_name",
                extracted_value="Shop",
                confidence=0.9,
                source_page=1,
                source_region="h",
            ),
        },
    )
    truth = {
        "fields": {
            "vendor_name": "Shop",
            "beneficiary": "Should not score for CONSUMIBLES",
        },
    }
    diff = evaluate(state, truth, store=store, date_parse="DMY")
    assert "beneficiary" not in diff["field_results"]
    assert diff["score"]["fields_total"] == 1
    assert diff["score"]["fields_wrong"] == 0


def test_evaluate_score_wrong_partial_exact():
    store = load_config("config/csv")
    state = AgentState(
        pdf_path="/tmp/x.pdf",
        output_dir="/tmp/out",
        invoice_type_id="CONSUMIBLES",
        extracted_fields={
            "vendor_name": FieldResult(
                field_id="c1",
                field_name="vendor_name",
                extracted_value="Wrong",
                confidence=0.9,
                source_page=1,
                source_region="h",
            ),
            "invoice_number": FieldResult(
                field_id="c2",
                field_name="invoice_number",
                extracted_value="123",
                confidence=0.9,
                source_page=1,
                source_region="h",
            ),
        },
    )
    truth = {
        "fields": {
            "vendor_name": "Wrong",
            "invoice_number": "999",
        },
    }
    diff = evaluate(state, truth, store=store, date_parse="DMY")
    s = diff["score"]
    assert s["fields_exact"] == 1
    assert s["fields_wrong"] == 1
    assert s["fields_partial"] == 0
    assert s["fields_total"] == 2
    assert s["fields_not_extracted"] == 0
    assert s["fields_wrong_value"] == 1
    assert s["exact_accuracy"] == 0.5
    assert s["field_accuracy"] == 0.5


def test_evaluate_score_splits_missing_vs_mismatch():
    store = load_config("config/csv")
    state = AgentState(
        pdf_path="/tmp/x.pdf",
        output_dir="/tmp/out",
        invoice_type_id="CONSUMIBLES",
        extracted_fields={
            "vendor_name": FieldResult(
                field_id="c1",
                field_name="vendor_name",
                extracted_value="Bad",
                confidence=0.9,
                source_page=1,
                source_region="h",
            ),
            "invoice_number": FieldResult(
                field_id="c2",
                field_name="invoice_number",
                extracted_value=None,
                confidence=0.0,
                source_page=1,
                source_region="h",
            ),
        },
    )
    truth = {
        "fields": {
            "vendor_name": "Good",
            "invoice_number": "123",
        },
    }
    diff = evaluate(state, truth, store=store, date_parse="DMY")
    s = diff["score"]
    assert s["fields_wrong"] == 2
    assert s["fields_not_extracted"] == 1
    assert s["fields_wrong_value"] == 1

