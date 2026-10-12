import pytest

from src.learning.evaluator import _compare_value
from src.tools.compliance_eval import _normalize_numeric
from src.tools.value_parsing import parse_amount, parse_full_date


@pytest.mark.parametrize(
    "text, expected",
    [
        ("425 000", 425000.0),
        ("425 000 FCFA", 425000.0),
        ("30 000 F CFA", 30000.0),
        ("50.000", 50000.0),
        ("50.000 R", 50000.0),
        ("2.366.181", 2366181.0),
        ("1.700.000", 1700000.0),
        ("3,758.00", 3758.0),
        ("$3,758.00", 3758.0),
        ("1.234,56", 1234.56),
        ("€ 1 190,00", 1190.0),
        ("Total: 1.190,00 EUR", 1190.0),
        ("1.234.56", 1234.56),
        ("12,5", 12.5),
        ("0.125", 0.125),
        ("0,500", 0.5),
        ("1234.56", 1234.56),
        ("19%", 19.0),
        ("<td>12,5</td>", 12.5),
        ("(120)", -120.0),
        ("-120", -120.0),
        (240000, 240000.0),
        (12.5, 12.5),
    ],
)
def test_parse_amount_printed_formats(text, expected):
    assert parse_amount(text) == expected


@pytest.mark.parametrize("text", [None, "", "null", "abc", True, float("nan")])
def test_parse_amount_no_number(text):
    assert parse_amount(text) is None


def test_parse_amount_dash_between_words_is_not_a_sign():
    assert parse_amount("Total - 120") == 120.0


def test_parse_amount_strict_rejects_references():
    assert parse_amount("006/09/2025", strict=True) is None
    assert parse_amount("N° 0007932", strict=True) is None
    assert parse_amount("0007932", strict=True) == 7932.0
    assert parse_amount("3758 USD", strict=True) == 3758.0
    assert parse_amount("50.000 R", strict=True) == 50000.0


def test_compliance_and_evaluator_use_the_same_parser():
    assert _normalize_numeric("50.000") == 50000.0
    assert _normalize_numeric("3,758.00") == 3758.0
    assert _compare_value("50000", "50.000", "total_amount")["match"] is True
    # Two references sharing a leading number must not match as numbers.
    assert _compare_value("006/10/2024", "006/09/2025", "invoice_number")["match"] is False
    assert _compare_value("0007932", "7932", "invoice_number")["match"] is True


@pytest.mark.parametrize(
    "text, expected",
    [
        ("31/07/2025", "2025-07-31"),
        ("31.07.25", "2025-07-31"),
        ("05/03/2025", "2025-03-05"),
        ("2025-11-09", "2025-11-09"),
        ("31 juillet 2025", "2025-07-31"),
        ("1er août 2025", "2025-08-01"),
        ("le 18 Septembre 2025", "2025-09-18"),
        ("N'Djamena, le 21 Aout 2025", "2025-08-21"),
        ("17 de enero de 2024", "2024-01-17"),
        ("21-Aug-2025", "2025-08-21"),
        ("31-Dec-2024", "2024-12-31"),
        ("December 31, 2024", "2024-12-31"),
    ],
)
def test_parse_full_date(text, expected):
    assert parse_full_date(text) == expected


@pytest.mark.parametrize("text", ["31", "28", "07/2025", "juillet 2025", "2025", "31/02/2025", "", None])
def test_parse_full_date_rejects_fragments(text):
    assert parse_full_date(text) is None


def test_parse_full_date_order_for_ambiguous_numbers():
    assert parse_full_date("05/03/2025", order="MDY") == "2025-05-03"
    assert parse_full_date("25/03/2025", order="MDY") == "2025-03-25"
