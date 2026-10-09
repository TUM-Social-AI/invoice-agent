"""Grounding: which OCR lines a field value or a visual verdict is tied to."""

from src.agent.state import FieldResult
from src.trace.evidence import Evidence, coerce_line_ids, parse_line_id
from src.trace.grounding import PageText, ground_field, ground_visual, text_matches


def _page(page, lines, w=1600, h=1400):
    return PageText(
        page=page, width=w, height=h,
        lines=[{"text": t, "confidence": 0.9, "bbox": list(b)} for t, b in lines],
    )


def _field(name, value, page, cited=None, source="model_cited"):
    ev = [Evidence(page=page, line_ids=cited, source=source)] if cited else []
    return FieldResult(
        field_id=name, field_name=name, extracted_value=value, confidence=0.9,
        source_page=page, source_region="header", evidence=ev,
    )


PAGE2 = _page(2, [
    ("DECHARGE DE PRIMES MENSUELLES", (300, 360, 1200, 400)),     # L0
    ("Montants", (1000, 470, 1100, 500)),                          # L1
    ("30 000 FCFA", (1000, 530, 1180, 560)),                       # L2  line amount
    ("TOTAL", (150, 580, 250, 610)),                               # L3
    ("30 000 FCFA", (1000, 570, 1180, 600)),                       # L4  total
    ("FACTURE DEFINITIVE N° 0253/2025", (100, 100, 600, 130)),     # L5
    ("Date: 05/03/2025", (700, 100, 950, 130)),                    # L6
])
META_TOTAL = {"type": "decimal", "label": "Total", "aliases": ["TOTAL"]}


def test_line_ids_are_parsed_and_normalized():
    assert parse_line_id("L12") == (None, 12)
    assert parse_line_id("[p2_L39]") == (2, 39)
    assert parse_line_id("line 3") == (None, None)
    assert coerce_line_ids(["L1", "p2_L3", "bogus", "L1"]) == ["L1", "p2_L3"]
    assert coerce_line_ids("L4, L5") == ["L4", "L5"]


def test_number_formats_match():
    assert text_matches("500250.0", "decimal", "Total: 500.250")
    assert text_matches("240000", "decimal", "240 000 FCFA")
    assert text_matches(1190.5, "decimal", "1.190,50 EUR")
    assert not text_matches("500250", "decimal", "Tel 50025")


def test_date_formats_match_but_not_the_year_alone():
    assert text_matches("2025-03-05", "date", "Date: 05/03/2025")
    assert text_matches("2025-03-05", "date", "le 05 mars 2025")
    # The invoice number contains 2025; it must not count as the date.
    assert not text_matches("2025-03-05", "date", "FACTURE DEFINITIVE N° 0253/2025")


def test_cited_lines_that_contain_the_value_are_confirmed():
    g = ground_field(_field("total_amount", "30000", 2, ["L4"]), META_TOTAL, {2: PAGE2})
    assert g.quality == "confirmed"
    assert g.line_ids == ["L4"]
    assert g.page == 2
    x1, y1, x2, y2 = g.boxes[0]
    assert abs(x1 - 1000 / 1600) < 0.01 and abs(x2 - 1180 / 1600) < 0.01
    assert abs(y1 - 570 / 1400) < 0.01 and abs(y2 - 600 / 1400) < 0.01


def test_duplicate_value_resolved_by_label_proximity():
    g = ground_field(_field("total_amount", "30000", 2), META_TOTAL, {2: PAGE2})
    assert g.quality == "matched"
    assert g.line_ids == ["L4"], "the line on the TOTAL row, not the line amount"


def test_duplicate_value_without_label_is_ambiguous():
    g = ground_field(_field("net_amount", "30000", 2), {"type": "decimal", "label": "Neto"}, {2: PAGE2})
    assert g.quality == "ambiguous"
    assert len(g.boxes) == 2


def test_value_on_another_page_is_found():
    other = _page(1, [("Beneficiaire: Relais", (10, 10, 400, 40))])
    f = _field("pay_period", "DECHARGE DE PRIMES MENSUELLES", 1)
    g = ground_field(f, {"type": "string", "label": "Periodo"}, {1: other, 2: PAGE2})
    assert g.quality == "matched" and g.page == 2
    assert "recorded page 1" in g.note


def test_invalid_citations_fall_back_to_search():
    g = ground_field(_field("invoice_date", "2025-03-05", 2, ["L99"]), {"type": "date", "label": "Date"}, {2: PAGE2})
    assert g.quality == "matched"
    assert g.line_ids == ["L6"]


def test_visual_citations_keep_page_and_cluster():
    trace = {"line_ids": ["p2_L3", "p2_L4", "p2_L0", "p7_L1"], "evidence_kind": "text"}
    out = ground_visual(trace, {2: PAGE2})
    assert all(g.page == 2 for g in out)
    # Heading, TOTAL label and amount are far apart → three boxes, not one spanning the page.
    assert len(out) == 3
    assert {tuple(g.line_ids) for g in out} == {("p2_L0",), ("p2_L3",), ("p2_L4",)}


def test_dates_match_across_languages_and_formats():
    assert text_matches("30 September 2025", "date", "Date 30 Septembre 2025")
    assert text_matches("2025-01-14", "date", "Fecha: 14 de enero de 2025")


def test_cited_number_tolerates_ocr_noise_but_search_does_not():
    assert text_matches("129000", "decimal", "129 0001", relaxed=True)
    assert not text_matches("129000", "decimal", "129 0001")


def test_far_apart_citations_become_separate_boxes():
    g = ground_field(_field("total_amount", "30000", 2, ["L2", "L0"]), META_TOTAL, {2: PAGE2})
    assert g.quality == "confirmed"
    assert len(g.boxes) == 1 and g.line_ids == ["L2"], "only the cluster that holds the value"


def test_numbers_do_not_match_by_digit_coincidence():
    assert not text_matches(100.0, "decimal", "Total 10.000 EUR")
    assert not text_matches("100", "decimal", "Ref 10000")
    assert not text_matches(250, "decimal", "25 000")
    assert not text_matches(12.5, "decimal", "PLZ 1250")
    assert not text_matches("10000", "decimal", "2024-10-0001", relaxed=True)
    assert text_matches(10000, "decimal", "Total 10.000 EUR")


def test_iso_date_is_not_read_as_day_month_year():
    from src.trace.grounding import _dates_in
    assert _dates_in("2024-03-12") == {"2024-03-12"}


def test_box_keeps_only_cited_lines_that_hold_the_value():
    # The model cites the total and the TOTAL label next to it; the box is the amount only.
    g = ground_field(_field("total_amount", "30000", 2, ["L3", "L4"]), META_TOTAL, {2: PAGE2})
    assert g.quality == "confirmed" and g.line_ids == ["L4"]
