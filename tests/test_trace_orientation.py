"""Sideways pages are turned by the quarter turn with the better OCR, or left alone when unsure."""

import io

import pytest

pytest.importorskip("pypdfium2")
pytest.importorskip("reportlab")

from reportlab.pdfgen import canvas  # noqa: E402

import src.trace.orientation as orientation  # noqa: E402
from src.agent.state import AgentState  # noqa: E402
from src.tools.ocr_layout import OcrLine, OcrResult  # noqa: E402


def _ocr(n, vertical_share, conf):
    lines = []
    for i in range(n):
        tall = i < round(n * vertical_share)
        bbox = (10, 10 * i, 20, 10 * i + 80) if tall else (10, 10 * i, 200, 10 * i + 12)
        lines.append(OcrLine(text=f"word{i}", confidence=conf, bbox=bbox))
    return OcrResult(lines=lines, image_width=600, image_height=800)


@pytest.fixture
def state(tmp_path):
    pdf = tmp_path / "doc.pdf"
    c = canvas.Canvas(str(pdf), pagesize=(595, 842))
    c.drawString(50, 50, "x")
    c.save()
    img = tmp_path / "page_001.jpg"
    from PIL import Image

    Image.new("RGB", (600, 800), "white").save(img)
    st = AgentState(pdf_path=str(pdf), output_dir=str(tmp_path))
    st.page_image_paths = [str(img)]
    st.page_count = 1
    return st


def _patch(monkeypatch, first, by_angle):
    monkeypatch.setattr(
        orientation, "detected_vertical_share",
        lambda models, image: (orientation.vertical_share(first), len(first.lines)),
    )
    monkeypatch.setattr(
        orientation, "_ocr_with_layout",
        lambda path, **k: by_angle[90 if path.endswith("_r90.jpg") else 270],
    )


def test_upright_page_is_left_alone(monkeypatch, state):
    _patch(monkeypatch, _ocr(30, 0.1, 0.9), {})
    assert orientation.fix_orientation(state, object(), dpi=72) == {}
    assert state.page_rotation == {}


def test_sideways_page_takes_the_turn_with_higher_confidence(monkeypatch, state):
    # Measured on A4048A p4: the right turn keeps 34% vertical lines (a sideways stamp).
    _patch(monkeypatch, _ocr(30, 0.76, 0.85), {90: _ocr(26, 0.27, 0.889), 270: _ocr(29, 0.34, 0.929)})
    assert orientation.fix_orientation(state, object(), dpi=72) == {1: 270}
    assert state.page_rotation == {1: 270}


def test_close_call_is_marked_uncertain(monkeypatch, state):
    _patch(monkeypatch, _ocr(30, 0.9, 0.85), {90: _ocr(30, 0.05, 0.880), 270: _ocr(30, 0.05, 0.885)})
    assert orientation.fix_orientation(state, object(), dpi=72) == {}
    assert state.orientation_uncertain == [1]
