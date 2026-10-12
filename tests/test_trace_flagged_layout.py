"""
The flagged PDF with and without summary pages, and the layout it returns for viewers
(page index in the file, scan size, margin, comment icons).
"""

import io

import pytest

pytest.importorskip("pypdfium2")
pytest.importorskip("pypdf")
pytest.importorskip("reportlab")

from pypdf import PdfReader  # noqa: E402
from reportlab.pdfgen import canvas  # noqa: E402

from src.trace.pdf_writer import MARGIN, _finding_comment_text, render_flagged_pdf  # noqa: E402


def write_source_pdf(path, pages=2) -> str:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(595, 842))
    for i in range(pages):
        c.drawString(72, 760, f"Invoice page {i + 1}")
        c.showPage()
    c.save()
    path.write_bytes(buf.getvalue())
    return str(path)


def _field(name, page, box, value="ACME", needs_review=False, quality="confirmed"):
    return {
        "field_name": name, "label": name.replace("_", " ").title(), "value": value, "confidence": 0.9,
        "needs_review": needs_review, "review_reason": None, "source_page": page,
        "evidence": {"page": page, "quality": quality, "boxes": [box] if box else [], "note": ""},
    }


def _finding(ref, status, page, box=None, expected=None):
    return {
        "ref": ref, "rule_id": f"R_{ref}", "rule_name": "rule", "status": status, "severity": "error",
        "message": "agent says", "error_message": f"{ref} is wrong", "requirement": "must be right",
        "field_names": [], "page": page,
        "evidence": [{"page": page, "boxes": [box]}] if box else [],
        "expected": expected,
    }


def sample_trace() -> dict:
    """Two pages: page 1 has a field and a failed rule, page 2 has nothing marked."""
    return {
        "document": {"file": "inv.pdf", "page_count": 2, "invoice_type_id": "CONSUMIBLES"},
        "run": {"status": "failed", "run_id": "r1"},
        "pages": [
            {"page": 1, "category": "INVOICE_HEADER", "description": "", "findings": 1, "text_boxes": []},
            {"page": 2, "category": "SUPPORTING_DOC", "description": "", "findings": 0, "text_boxes": []},
        ],
        "fields": [
            _field("vendor_name", 1, [0.1, 0.1, 0.4, 0.15]),
            _field("total_amount", 1, None, value=425000.0, quality="none"),
            _field("vat_amount", None, None, value=None, quality="none"),
        ],
        "findings": [
            _finding("F1", "failed", 1, box=[0.5, 0.5, 0.7, 0.55]),
            _finding("F2", "warning", 1, expected={"page": 1, "boxes": [[0.2, 0.8, 0.4, 0.85]], "note": "stamp"}),
            _finding("F3", "passed", 1),
        ],
        "summary": {"general_fields": ["vendor_name"], "counts": {"failed": 1, "warning": 1, "passed": 1}},
    }


@pytest.mark.parametrize("include_summary", [True, False])
def test_summary_pages_are_optional(tmp_path, include_summary):
    src = write_source_pdf(tmp_path / "in.pdf")
    out = tmp_path / "out.pdf"
    layout = render_flagged_pdf(sample_trace(), src, str(out), include_summary=include_summary)
    n = len(PdfReader(str(out)).pages)
    if include_summary:
        assert layout["summary_pages"] >= 1
        assert n == 2 + layout["summary_pages"]
    else:
        assert layout["summary_pages"] == 0
        assert n == 2
    assert [p["pdf_index"] for p in layout["pages"]] == [layout["summary_pages"], layout["summary_pages"] + 1]


def test_layout_reports_margin_and_comment_icons(tmp_path):
    src = write_source_pdf(tmp_path / "in.pdf")
    trace = sample_trace()
    layout = render_flagged_pdf(trace, src, str(tmp_path / "out.pdf"), include_summary=False)
    p1, p2 = layout["pages"]
    assert (p1["scan_w"], p1["scan_h"], p1["margin"]) == (595.0, 842.0, MARGIN)
    assert p2["margin"] == 0 and p2["comments"] == []

    # Passed findings get no comment; the others are in the margin strip with the PDF's text.
    assert [c["ref"] for c in p1["comments"]] == ["F1", "F2"]
    for c in p1["comments"]:
        x1, y1, x2, y2 = c["rect"]
        assert p1["scan_w"] < x1 < x2 <= p1["scan_w"] + MARGIN
        assert 0 <= y1 < y2 <= p1["scan_h"]
    by_ref = {f["ref"]: f for f in trace["findings"]}
    assert p1["comments"][0]["text"] == _finding_comment_text(by_ref["F1"])

    # The comment icons in the file sit where the layout says.
    page = PdfReader(str(tmp_path / "out.pdf")).pages[0]
    annots = [a.get_object() for a in page["/Annots"]]
    texts = [a for a in annots if a["/Subtype"] == "/Text"]
    assert len(texts) == 2
    left, bottom = float(texts[0]["/Rect"][0]), float(texts[0]["/Rect"][1])
    x1, y1, x2, y2 = p1["comments"][0]["rect"]
    assert left == pytest.approx(x1, abs=0.01)
    assert bottom == pytest.approx(p1["scan_h"] - y2, abs=0.01)


@pytest.mark.parametrize("summary_pages", [True, False])
def test_finalize_stores_the_layout_in_the_trace(tmp_path, monkeypatch, summary_pages):
    import json
    from types import SimpleNamespace

    from src.trace import finalize

    monkeypatch.setattr(finalize, "build_trace", lambda *a, **k: sample_trace())
    state = SimpleNamespace(
        output_dir=str(tmp_path / "out"), pdf_path=write_source_pdf(tmp_path / "inv.pdf"),
        page_count=2, page_image_paths=[], page_rotation=None,
    )
    config = {"traceability": {"enabled": True, "summary_pages": summary_pages}}
    paths = finalize.write_trace_outputs(state, None, config)

    trace = json.loads(open(paths["trace_json"], encoding="utf-8").read())
    layout = trace["flagged_pdf"]
    assert layout["file"] == "flagged_inv.pdf"
    assert (layout["summary_pages"] > 0) is summary_pages
    assert len(PdfReader(paths["flagged_pdf"]).pages) == 2 + layout["summary_pages"]
