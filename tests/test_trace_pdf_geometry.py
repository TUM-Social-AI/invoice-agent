"""
Boxes given as fractions of the upright page must land at that spot after pdfium renders
the flagged page, for every /Rotate value and for a CropBox that is not at the origin.
"""

import io

import pytest

pdfium = pytest.importorskip("pypdfium2")
pypdf = pytest.importorskip("pypdf")
pytest.importorskip("reportlab")

from pypdf import PdfReader, PdfWriter  # noqa: E402
from pypdf.generic import RectangleObject  # noqa: E402
from reportlab.pdfgen import canvas  # noqa: E402

from src.trace.pdf_writer import Mark, Overlay, PageGeom  # noqa: E402


def _blank_pdf(w=595, h=842) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(w, h))
    c.drawString(10, 10, ".")
    c.save()
    return buf.getvalue()


def _red_bbox(img):
    px = img.convert("RGB").load()
    xs, ys = [], []
    for y in range(0, img.height):
        for x in range(0, img.width):
            r, g, b = px[x, y]
            if r > 150 and g < 90 and b < 90:
                xs.append(x)
                ys.append(y)
    assert xs, "no red pixels rendered"
    return min(xs) / img.width, min(ys) / img.height, max(xs) / img.width, max(ys) / img.height


@pytest.mark.parametrize("rotate", [0, 90, 180, 270])
@pytest.mark.parametrize("cropped", [False, True])
def test_box_lands_where_the_upright_page_shows_it(rotate, cropped):
    writer = PdfWriter()
    writer.append(PdfReader(io.BytesIO(_blank_pdf())))
    page = writer.pages[0]
    if cropped:
        page.cropbox = RectangleObject((40, 60, 540, 800))
    if rotate:
        page.rotate(rotate)
    nbox = [0.55, 0.10, 0.90, 0.20]
    ov = Overlay(page)
    ov.add(Mark(nbox, (1, 0, 0), "", width=2.0))
    page.merge_page(ov.page_object())
    out = io.BytesIO()
    writer.write(out)

    img = pdfium.PdfDocument(out.getvalue())[0].render(scale=1).to_pil()
    got = _red_bbox(img)
    for a, b in zip(got, nbox):
        assert abs(a - b) < 0.012, (rotate, cropped, got, nbox)


def test_display_size_swaps_for_quarter_turns():
    writer = PdfWriter()
    writer.append(PdfReader(io.BytesIO(_blank_pdf())))
    page = writer.pages[0]
    page.rotate(90)
    g = PageGeom.of(page)
    assert (round(g.disp_w), round(g.disp_h)) == (842, 595)


@pytest.mark.parametrize("rotate", [0, 90, 180, 270])
def test_margin_strip_is_added_right_of_the_displayed_page(rotate):
    from src.trace.pdf_writer import MARGIN, extend_right_margin

    writer = PdfWriter()
    writer.append(PdfReader(io.BytesIO(_blank_pdf())))
    page = writer.pages[0]
    if rotate:
        page.rotate(rotate)
    nbox = [0.55, 0.10, 0.90, 0.20]
    ov = Overlay(page)
    g = ov.g
    ov.add(Mark(nbox, (1, 0, 0), "", width=2.0))
    ov.legend.append((0.0, "legend entry", (1, 0, 0)))  # drawn in the margin
    extend_right_margin(page, g)
    page.merge_page(ov.page_object(page.mediabox))
    out = io.BytesIO()
    writer.write(out)

    img = pdfium.PdfDocument(out.getvalue())[0].render(scale=1).to_pil()
    assert abs(img.width - (g.disp_w + MARGIN)) <= 1 and abs(img.height - g.disp_h) <= 1
    k = g.disp_w / (g.disp_w + MARGIN)  # the scan keeps its size; only the width grows
    scan = img.crop((0, 0, int(img.width * k) - 2, img.height))
    x1, y1, x2, y2 = _red_bbox(scan)
    for got, want in zip((x1 * k, y1, x2 * k, y2), (nbox[0] * k, nbox[1], nbox[2] * k, nbox[3])):
        assert abs(got - want) < 0.012, (rotate, (x1, y1, x2, y2))
    margin = img.crop((int(img.width * k) + 2, 0, img.width, img.height)).convert("RGB")
    reddish = [px for px in margin.getdata() if px[0] - px[1] > 40]  # small text renders anti-aliased
    assert reddish, "legend text in the margin is not clipped"
