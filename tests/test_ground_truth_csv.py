import csv
from pathlib import Path

from src.learning.evaluator import load_ground_truth


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

