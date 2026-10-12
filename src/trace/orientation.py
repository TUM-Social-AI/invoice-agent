"""
Detect pages scanned sideways and turn them upright before extraction.

Surya reads rotated text, so its text alone cannot tell which way is up; the shape of its
line boxes can. Surya's text detector (no recognition, ~0.5 s per page) shows whether most
text boxes are taller than wide, i.e. the page is sideways. For such a page, both quarter
turns are OCR'd and the one with the higher mean OCR confidence wins,
but only if most of its lines then run horizontally. Anything less certain leaves the page as it
is and records it as uncertain. Upside-down pages are not detected.

The fix is a clockwise angle per page in `state.page_rotation`. Page renders and the
flagged PDF apply the same angle (pdfium's `rotation` and pypdf's `rotate` turn the same
way), so boxes found on the upright render land correctly in the PDF.
"""

from __future__ import annotations

import logging
import statistics
from pathlib import Path
from typing import Any

from src.tools.ocr_layout import OcrResult, _ocr_with_layout
from src.trace.ocr_cache import _cache_path

logger = logging.getLogger(__name__)

MIN_LINES = 8           # too little text → do not judge
SIDEWAYS_SHARE = 0.6    # share of vertical lines that marks a page as sideways
# After the turn, at most this share may still be vertical. Not lower: stamps and margin
# notes printed sideways keep 25-35% of a correctly turned page vertical.
UPRIGHT_SHARE = 0.5
MIN_CONF_MARGIN = 0.02  # 90 vs 270: confidence gap needed to pick one


def _text_lines(ocr: OcrResult) -> list:
    return [l for l in ocr.lines if len(l.text.strip()) >= 4]


def vertical_share(ocr: OcrResult) -> float:
    ls = _text_lines(ocr)
    if not ls:
        return 0.0
    tall = sum(1 for l in ls if (l.bbox[3] - l.bbox[1]) > (l.bbox[2] - l.bbox[0]))
    return tall / len(ls)


def mean_confidence(ocr: OcrResult) -> float:
    ls = _text_lines(ocr)
    return statistics.mean(l.confidence for l in ls) if ls else 0.0


DETECT_DPI = 100   # text-line detection only: ~0.5 s per page, enough to see line shapes
COMPARE_DPI = 120  # OCR of both quarter turns, only for pages that look sideways


def detected_vertical_share(surya_models: Any, image) -> tuple[float, int]:
    """(share of tall text boxes, number of boxes) from surya's detector, without recognition."""
    result = surya_models.det_predictor([image])
    boxes = [b.bbox for b in result[0].bboxes if max(b.bbox[3] - b.bbox[1], b.bbox[2] - b.bbox[0]) > 15]
    if not boxes:
        return 0.0, 0
    tall = sum(1 for b in boxes if (b[3] - b[1]) > (b[2] - b[0]))
    return tall / len(boxes), len(boxes)


def fix_orientation(state: Any, surya_models: Any, dpi: int, silent: bool = False) -> dict[int, int]:
    """Turn sideways pages upright in place (page image re-rendered). Returns {page: angle} applied."""
    if surya_models is None or not state.page_image_paths:
        return {}
    import pypdfium2 as pdfium

    applied: dict[int, int] = {}
    uncertain: list[int] = list(getattr(state, "orientation_uncertain", []) or [])
    checked: list[int] = list(getattr(state, "orientation_checked", []) or [])
    pdf = pdfium.PdfDocument(state.pdf_path)
    try:
        for p, img_path in enumerate(state.page_image_paths, start=1):
            if p in state.page_rotation or p in uncertain or p in checked:
                continue  # decided on an earlier render
            checked.append(p)
            page = pdf[p - 1]
            share, n = detected_vertical_share(surya_models, page.render(scale=DETECT_DPI / 72.0).to_pil().convert("RGB"))
            if n < MIN_LINES or share < SIDEWAYS_SHARE:
                continue
            candidates = []
            for angle in (90, 270):
                tmp = Path(state.tmp_dir) / "orientation" / f"page_{p:03d}_r{angle}.jpg"
                tmp.parent.mkdir(parents=True, exist_ok=True)
                im = page.render(scale=COMPARE_DPI / 72.0, rotation=angle).to_pil().convert("RGB")
                w, h = im.size
                # The central area decides as well as the full page (checked on the sample
                # invoices) and saves OCR time on dense tables.
                im.crop((int(w * 0.15), int(h * 0.2), int(w * 0.85), int(h * 0.8))).save(tmp, "JPEG", quality=85)
                o = _ocr_with_layout(str(tmp), surya_models=surya_models, silent=silent)
                candidates.append((mean_confidence(o), vertical_share(o), angle))
            ok = sorted([c for c in candidates if c[1] <= UPRIGHT_SHARE], key=lambda c: c[0], reverse=True)
            margin = ok[0][0] - ok[1][0] if len(ok) > 1 else 1.0
            logger.info(
                "orientation p%d: %.0f%% vertical text boxes → %s", p, share * 100,
                ", ".join(f"{c[2]}°: OCR conf {c[0]:.3f}, vertical {c[1]:.0%}" for c in candidates),
            )
            if not ok or margin < MIN_CONF_MARGIN:
                uncertain.append(p)
                logger.info("orientation p%d: uncertain, page left as scanned", p)
                continue
            conf, _, angle = ok[0]
            # Re-render the full-res page image upright; its OCR is made later from this image.
            im = page.render(scale=dpi / 72.0, rotation=angle).to_pil()
            if im.mode in ("RGBA", "P"):
                im = im.convert("RGB")
            im.save(img_path, "JPEG", quality=85, optimize=True)
            cache = _cache_path(state, p)
            if cache.exists():
                cache.unlink()
            state.page_rotation[p] = angle
            applied[p] = angle
            logger.info("orientation p%d: turned %d° clockwise (OCR conf %.3f, margin %.3f)", p, angle, conf, margin)
    finally:
        pdf.close()
    state.orientation_uncertain = uncertain
    state.orientation_checked = checked
    return applied
