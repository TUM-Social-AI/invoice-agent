"""
One surya OCR result per page, persisted under `<output_dir>/tmp/ocr/` and reused.

Reusing one result per page keeps line IDs stable across extraction retries, crops,
the visual check and the post-run exporter. The OCR always runs on the full-res page
render (never a medium image or a 48 DPI thumbnail), and boxes are rescaled on load to
whatever image of that page the caller uses, because the crop path needs pixel boxes
for its own image. A record is only reused for the same PDF file and page rotation.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

from PIL import Image

from src.tools.ocr_layout import OcrLine, OcrResult, _ocr_with_layout

logger = logging.getLogger(__name__)


def _cache_dir(state: Any) -> Path:
    return Path(state.output_dir) / "tmp" / "ocr"


def _cache_path(state: Any, page_num: int) -> Path:
    return _cache_dir(state) / f"page_{page_num:03d}.json"


def _source_key(state: Any) -> str:
    """Identifies the PDF the OCR was made from (size + mtime; cheap, no hashing)."""
    try:
        st = Path(state.pdf_path).stat()
        return f"{st.st_size}:{int(st.st_mtime)}"
    except OSError:
        return ""


def _rotation(state: Any, page_num: int) -> int:
    return int((getattr(state, "page_rotation", None) or {}).get(page_num, 0))


def load_page_ocr_record(state: Any, page_num: int) -> Optional[dict]:
    """Cached record {page, image_path, width, height, lines:[{text, confidence, bbox}]} or None if stale."""
    p = _cache_path(state, page_num)
    if not p.exists():
        return None
    try:
        record = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("OCR cache for page %d unreadable: %s", page_num, e)
        return None
    if record.get("source_key") != _source_key(state) or int(record.get("rotation", 0)) != _rotation(state, page_num):
        return None  # other file or other orientation: boxes would not line up
    return record


def clear_page_ocr_cache(state: Any) -> None:
    """Drop all cached OCR of this output dir (start of a run: the dir may hold an older run)."""
    d = _cache_dir(state)
    if d.is_dir():
        for f in d.glob("page_*.json"):
            try:
                f.unlink()
            except OSError:
                pass


def _canonical_image(state: Any, page_num: int) -> Optional[str]:
    """The full-res render of the page, or None while only 48 DPI thumbnails exist."""
    paths = state.page_image_paths or []
    if not (1 <= page_num <= len(paths)):
        return None
    if state.compressed_page_paths and paths == state.compressed_page_paths:
        return None
    return paths[page_num - 1]


def _record_to_result(record: dict, target_size: Optional[tuple[int, int]] = None) -> OcrResult:
    w, h = int(record.get("width") or 0), int(record.get("height") or 0)
    sx = sy = 1.0
    if target_size and w and h:
        sx, sy = target_size[0] / w, target_size[1] / h
        w, h = target_size
    lines = []
    for ln in record.get("lines", []):
        b = ln["bbox"]
        lines.append(
            OcrLine(
                text=ln.get("text", ""),
                confidence=float(ln.get("confidence", 0.0)),
                bbox=(int(b[0] * sx), int(b[1] * sy), int(b[2] * sx), int(b[3] * sy)),
            )
        )
    return OcrResult(lines=lines, image_width=w, image_height=h)


def page_ocr(
    state: Any,
    page_num: int,
    image_path: str,
    surya_models: Any,
    silent: bool = False,
) -> OcrResult:
    """OCR for `page_num`, from cache when present, scaled to `image_path`'s pixel size."""
    try:
        with Image.open(image_path) as im:
            size = im.size
    except OSError:
        size = None

    record = load_page_ocr_record(state, page_num)
    if record is not None:
        return _record_to_result(record, size)

    canonical = _canonical_image(state, page_num)
    if canonical is None:
        # No full-res render yet: OCR what we were given, but do not cache a low-quality result.
        return _ocr_with_layout(image_path, surya_models=surya_models, silent=silent)
    ocr = _ocr_with_layout(canonical, surya_models=surya_models, silent=silent)
    if ocr.is_empty():
        # Do not cache failures: a later call (or a later run of surya) may succeed.
        return ocr
    record = {
        "page": page_num,
        "image_path": str(canonical),
        "width": ocr.image_width,
        "height": ocr.image_height,
        "rotation": _rotation(state, page_num),
        "source_key": _source_key(state),
        "lines": [{"text": l.text, "confidence": l.confidence, "bbox": list(l.bbox)} for l in ocr.lines],
    }
    path = _cache_path(state, page_num)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        logger.warning("Could not write OCR cache for page %d: %s", page_num, e)
    return _record_to_result(record, size)


def lines_in_box(ocr: OcrResult, box: tuple) -> list[str]:
    """IDs (`L<n>`) of non-blank OCR lines whose centre lies inside `box` (pixels)."""
    from src.trace.evidence import line_id

    x1, y1, x2, y2 = box
    out = []
    for i, l in enumerate(ocr.lines):
        if not l.text.strip():
            continue
        cx, cy = (l.bbox[0] + l.bbox[2]) / 2, (l.bbox[1] + l.bbox[3]) / 2
        if x1 <= cx <= x2 and y1 <= cy <= y2:
            out.append(line_id(i))
    return out


def format_lines_with_ids(
    ocr: OcrResult,
    indices: Optional[list[int]] = None,
    page: Optional[int] = None,
    max_chars: int = 0,
) -> str:
    """OCR lines as `[L12] text` (or `[p2_L12] text`); see `lines_with_ids`."""
    return lines_with_ids(ocr, indices, page, max_chars)[0]


def lines_with_ids(
    ocr: OcrResult,
    indices: Optional[list[int]] = None,
    page: Optional[int] = None,
    max_chars: int = 0,
) -> tuple[str, set[str]]:
    """
    (prompt text, IDs shown). One line per OCR line, blank lines skipped.

    The cap drops whole lines from the middle so every remaining line keeps its ID
    (a character cut would leave lines whose ID the model cannot cite). The returned
    ID set is what the model saw, so citations of anything else can be discarded.
    """
    from src.trace.evidence import line_id

    idxs = list(range(len(ocr.lines))) if indices is None else list(indices)
    pairs = [(line_id(i, page), f"[{line_id(i, page)}] {ocr.lines[i].text.strip()}") for i in idxs if ocr.lines[i].text.strip()]
    rows = [r for _, r in pairs]
    text = "\n".join(rows)
    if max_chars <= 0 or len(text) <= max_chars:
        return text, {lid for lid, _ in pairs}
    marker = "[... OCR lines omitted for prompt size ...]"
    budget = max(0, max_chars - len(marker) - 2)
    head: list[str] = []
    tail: list[str] = []
    used = 0
    i, j = 0, len(rows) - 1
    take_head = True
    while i <= j:
        row = rows[i] if take_head else rows[j]
        if used + len(row) + 1 > budget:
            break
        if take_head:
            head.append(row)
            i += 1
        else:
            tail.insert(0, row)
            j -= 1
        used += len(row) + 1
        take_head = not take_head
    kept = head + tail
    shown = {lid for lid, r in pairs if r in set(kept)}
    return "\n".join(head + [marker] + tail), shown
