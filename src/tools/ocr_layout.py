"""Moved implementations for ocr_layout.py."""

import logging
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from PIL import Image
from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)


@dataclass
class SuryaModels:
    """Holds pre-loaded surya OCR model objects so they are loaded only once at startup."""
    det_predictor: Any
    rec_predictor: Any


@dataclass
class PaddleOcrModels:
    """Holds a pre-loaded PaddleOCR pipeline."""
    ocr: Any
    lang: str
    engine: str = "paddle"


@dataclass
class OcrEngine:
    """Runtime OCR backend selected from config."""
    backend: str
    models: Any
    langs: list[str]


def load_surya_models(auto_install: bool = True) -> "Optional[SuryaModels]":
    """
    Load surya OCR detection + recognition models.
    If surya-ocr is not installed, attempts a one-time automatic installation
    before giving up. Models are ~300 MB and download automatically on first use;
    loading takes ~5-15s and is cached internally for the rest of the session.

    Supports both the legacy API (<=0.6: RecognitionPredictor()) and the current
    API (>=0.7: RecognitionPredictor(FoundationPredictor())).
    """
    def _import_and_load():
        from surya.detection import DetectionPredictor
        from surya.recognition import RecognitionPredictor
        from surya.foundation import FoundationPredictor
        logger.info("Loading surya OCR models (one-time, ~5-15s)…")
        det = DetectionPredictor()
        # surya >= 0.7: RecognitionPredictor requires a FoundationPredictor instance.
        # Requires transformers < 5 — surya 0.17.x is incompatible with transformers 5.x
        # (see https://github.com/datalab-to/surya/issues/484).
        # Fix: pip install "transformers>=4.56.1,<5"
        fp = FoundationPredictor()
        rec = RecognitionPredictor(fp)
        logger.info("Surya OCR models ready")
        return SuryaModels(det_predictor=det, rec_predictor=rec)

    try:
        return _import_and_load()
    except ImportError:
        if not auto_install:
            logger.warning(
                "surya-ocr not installed - OCR pre-pass disabled. "
                "Install manually: pip install surya-ocr"
            )
            return None
        logger.info("surya-ocr not installed — attempting automatic installation…")
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", "surya-ocr"],
                capture_output=True,
                text=True,
                timeout=300,
            )
            if result.returncode != 0:
                logger.warning(
                    f"surya-ocr installation failed — OCR pre-pass disabled.\n"
                    f"Run manually: pip install surya-ocr\n"
                    f"pip stderr: {result.stderr[-200:]}"
                )
                return None
            logger.info("surya-ocr installed successfully — loading models…")
            return _import_and_load()
        except Exception as e:
            logger.warning(f"surya-ocr auto-install failed: {e} — OCR pre-pass disabled")
            return None
    except Exception as e:
        logger.warning(f"Failed to load surya models: {e} — OCR pre-pass disabled")
        return None


def _paddle_lang_from_configured_langs(langs: list[str]) -> str:
    """Choose a PaddleOCR language code from the configured OCR languages."""
    cleaned = [str(x).strip().lower() for x in langs if str(x).strip()]
    if not cleaned:
        return "en"
    latin_langs = {"es", "fr", "de", "it", "pt", "ca", "nl", "pl", "ro"}
    if any(lang in latin_langs for lang in cleaned):
        return "latin"
    return cleaned[0]


def load_paddleocr_models(config: dict | None = None) -> "Optional[PaddleOcrModels]":
    """
    Load PaddleOCR once and keep the pipeline for the full agent session.

    PaddleOCR 3.x uses ``predict(...)`` with document preprocessing flags, while
    PaddleOCR 2.x uses ``ocr(...)`` and ``use_angle_cls``.  The loader tries the
    newer constructor first and falls back to the legacy signature when needed.
    """
    cfg = config or {}
    ocr_cfg = cfg.get("ocr", {}) or {}
    paddle_cfg = ocr_cfg.get("paddleocr", {}) or {}
    langs = ocr_cfg.get("langs", ["es", "en"]) or ["es", "en"]
    lang = str(
        paddle_cfg.get("lang")
        or ocr_cfg.get("paddle_lang")
        or _paddle_lang_from_configured_langs(langs)
    ).strip()
    engine = str(paddle_cfg.get("engine", "paddle") or "paddle").strip()
    use_textline_orientation = bool(paddle_cfg.get("use_textline_orientation", False))

    try:
        from paddleocr import PaddleOCR
    except ImportError:
        logger.warning(
            "paddleocr is not installed - OCR pre-pass disabled. "
            "Install manually: pip install paddleocr paddlepaddle"
        )
        return None

    constructors = [
        {
            "lang": lang,
            "use_doc_orientation_classify": False,
            "use_doc_unwarping": False,
            "use_textline_orientation": use_textline_orientation,
            "engine": engine,
        },
        {
            "lang": lang,
            "use_doc_orientation_classify": False,
            "use_doc_unwarping": False,
            "use_textline_orientation": use_textline_orientation,
        },
        {
            "lang": lang,
            "use_angle_cls": use_textline_orientation,
        },
    ]

    last_error: Exception | None = None
    for kwargs in constructors:
        try:
            logger.info("Loading PaddleOCR backend (lang=%s, engine=%s)", lang, engine)
            return PaddleOcrModels(ocr=PaddleOCR(**kwargs), lang=lang, engine=engine)
        except TypeError as e:
            last_error = e
            continue
        except Exception as e:
            logger.warning("Failed to load PaddleOCR backend: %s - OCR pre-pass disabled", e)
            return None

    logger.warning(
        "Failed to load PaddleOCR backend: %s - OCR pre-pass disabled",
        last_error or "unsupported PaddleOCR constructor",
    )
    return None


def load_ocr_engine(config: dict | None = None) -> "Optional[OcrEngine]":
    """Load the OCR backend selected by ``ocr.backend`` in config."""
    cfg = config or {}
    ocr_cfg = cfg.get("ocr", {}) or {}
    backend = str(ocr_cfg.get("backend", "surya") or "surya").strip().lower()
    backend = {"paddle": "paddleocr", "paddle_ocr": "paddleocr", "none": "disabled"}.get(
        backend,
        backend,
    )
    langs = [str(x) for x in (ocr_cfg.get("langs", ["es", "en"]) or ["es", "en"])]

    if backend in {"disabled", "disable", "off", "false"}:
        logger.info("OCR pre-pass disabled by config")
        return None
    if backend == "auto":
        paddle_models = load_paddleocr_models(cfg)
        if paddle_models is not None:
            return OcrEngine(backend="paddleocr", models=paddle_models, langs=langs)
        surya_models = load_surya_models(auto_install=bool(ocr_cfg.get("auto_install", False)))
        if surya_models is not None:
            return OcrEngine(backend="surya", models=surya_models, langs=langs)
        return None
    if backend == "paddleocr":
        models = load_paddleocr_models(cfg)
        return OcrEngine(backend=backend, models=models, langs=langs) if models is not None else None
    if backend == "surya":
        models = load_surya_models(auto_install=bool(ocr_cfg.get("auto_install", False)))
        return OcrEngine(backend=backend, models=models, langs=langs) if models is not None else None

    logger.warning("Unknown OCR backend '%s' - OCR pre-pass disabled", backend)
    return None


class OcrLine(BaseModel):
    model_config = ConfigDict(extra="forbid")
    """Single OCR text line with its pixel-space bounding box."""
    text: str
    confidence: float
    bbox: tuple[int, int, int, int]

class OcrResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    """Full-page OCR output: structured lines with bboxes + flat text."""
    lines: list[OcrLine]
    image_width: int = 0
    image_height: int = 0

    @property
    def full_text(self) -> str:
        return "\n".join(l.text for l in self.lines if l.text.strip())

    def is_empty(self) -> bool:
        return not self.lines

class FieldLocalization(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)
    """Result of finding a field's label and value region in the OCR layout."""
    label_line: Any          # OcrLine where the label was found
    value_text: Optional[str]  # Extracted value text, None if blank/handwritten
    value_bbox: tuple[int, int, int, int]
    value_confidence: float  # OCR confidence; 0 if value not found/readable

def _ocr_with_layout(
    image_path: str,
    ocr_engine: "Optional[OcrEngine]" = None,
    surya_models: "Optional[SuryaModels]" = None,
) -> OcrResult:
    """
    Run the configured OCR backend and return structured results with bounding boxes.
    Each OcrLine has .text, .confidence, and .bbox (pixel coords).
    Returns empty OcrResult if OCR is unavailable or fails.
    """
    if ocr_engine is None and surya_models is not None:
        ocr_engine = OcrEngine(backend="surya", models=surya_models, langs=[])
    if ocr_engine is None or ocr_engine.models is None:
        return OcrResult(lines=[])
    if ocr_engine.backend == "paddleocr":
        return _ocr_with_paddleocr(image_path, ocr_engine.models)
    if ocr_engine.backend != "surya":
        logger.debug("Unsupported OCR backend '%s'", ocr_engine.backend)
        return OcrResult(lines=[])
    try:
        from surya.common.surya.schema import TaskNames
        img = Image.open(image_path)
        w, h = img.size
        models = ocr_engine.models
        predictions = models.rec_predictor(
            images=[img],
            task_names=[TaskNames.ocr_without_boxes],
            det_predictor=models.det_predictor,
        )
        lines = []
        for tl in predictions[0].text_lines:
            b = tl.bbox  # [x1, y1, x2, y2] floats
            lines.append(OcrLine(
                text=tl.text.strip(),
                confidence=float(tl.confidence or 0),
                bbox=(int(b[0]), int(b[1]), int(b[2]), int(b[3])),
            ))
        return OcrResult(lines=lines, image_width=w, image_height=h)
    except Exception as e:
        logger.debug(f"Surya OCR failed for {image_path}: {e}")
        return OcrResult(lines=[])


def _plain_paddle_value(value: Any) -> Any:
    """Convert PaddleOCR/numpy values to plain Python containers."""
    if hasattr(value, "tolist"):
        return value.tolist()
    if isinstance(value, dict):
        return {k: _plain_paddle_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_paddle_value(v) for v in value]
    return value


def _paddle_result_payload(result: Any) -> dict:
    if isinstance(result, dict):
        return _plain_paddle_value(result)
    json_attr = getattr(result, "json", None)
    if callable(json_attr):
        try:
            return _plain_paddle_value(json_attr())
        except TypeError:
            pass
    if isinstance(json_attr, dict):
        return _plain_paddle_value(json_attr)
    res_attr = getattr(result, "res", None)
    if isinstance(res_attr, dict):
        return {"res": _plain_paddle_value(res_attr)}
    return {}


def _bbox_from_paddle_box(box: Any) -> tuple[int, int, int, int] | None:
    box = _plain_paddle_value(box)
    if not isinstance(box, list) or not box:
        return None
    if len(box) == 4 and all(isinstance(v, (int, float)) for v in box):
        x1, y1, x2, y2 = box
        return (int(x1), int(y1), int(x2), int(y2))
    points = []
    for pt in box:
        if isinstance(pt, (list, tuple)) and len(pt) >= 2:
            points.append((float(pt[0]), float(pt[1])))
    if not points:
        return None
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys)))


def _paddle_lines_from_v3_result(result: Any) -> list[OcrLine]:
    payload = _paddle_result_payload(result)
    res = payload.get("res", payload)
    texts = res.get("rec_texts") or res.get("texts") or []
    scores = res.get("rec_scores") or res.get("scores") or []
    boxes = res.get("rec_boxes") or res.get("rec_polys") or res.get("dt_polys") or []
    lines: list[OcrLine] = []
    for idx, text in enumerate(texts):
        text_s = str(text).strip()
        if not text_s:
            continue
        bbox = _bbox_from_paddle_box(boxes[idx]) if idx < len(boxes) else None
        if bbox is None:
            continue
        score = scores[idx] if idx < len(scores) else 0.0
        try:
            confidence = float(score or 0)
        except (TypeError, ValueError):
            confidence = 0.0
        lines.append(OcrLine(text=text_s, confidence=confidence, bbox=bbox))
    return lines


def _paddle_lines_from_legacy_result(result: Any) -> list[OcrLine]:
    result = _plain_paddle_value(result)
    if not isinstance(result, list):
        return []
    candidates = result
    if len(result) == 1 and isinstance(result[0], list):
        candidates = result[0]
    lines: list[OcrLine] = []
    for item in candidates:
        if not isinstance(item, list) or len(item) < 2:
            continue
        bbox = _bbox_from_paddle_box(item[0])
        rec = item[1]
        if bbox is None or not isinstance(rec, (list, tuple)) or not rec:
            continue
        text_s = str(rec[0]).strip()
        if not text_s:
            continue
        try:
            confidence = float(rec[1] if len(rec) > 1 else 0)
        except (TypeError, ValueError):
            confidence = 0.0
        lines.append(OcrLine(text=text_s, confidence=confidence, bbox=bbox))
    return lines


def _ocr_with_paddleocr(image_path: str, paddle_models: PaddleOcrModels) -> OcrResult:
    try:
        img = Image.open(image_path)
        w, h = img.size
        ocr = paddle_models.ocr
        lines: list[OcrLine] = []
        if hasattr(ocr, "predict"):
            predictions = ocr.predict(image_path)
            for pred in predictions or []:
                lines.extend(_paddle_lines_from_v3_result(pred))
        if not lines and hasattr(ocr, "ocr"):
            try:
                predictions = ocr.ocr(image_path, cls=False)
            except TypeError:
                predictions = ocr.ocr(image_path)
            lines.extend(_paddle_lines_from_legacy_result(predictions))
        lines.sort(key=lambda line: (line.bbox[1], line.bbox[0]))
        return OcrResult(lines=lines, image_width=w, image_height=h)
    except Exception as e:
        logger.debug("PaddleOCR failed for %s: %s", image_path, e)
        return OcrResult(lines=[])


def _localize_field_in_ocr(
    field_meta: dict,
    ocr: OcrResult,
) -> Optional[FieldLocalization]:
    """
    Search the OCR layout for a field's label and identify its adjacent value region.

    Tries three patterns in order:
      1. "Label: Value" on the same line (value extracted from text)
      2. "Label:" with value on the next line that overlaps horizontally
      3. Label only found — extend the bbox downward to capture blank/handwritten value

    Returns None if the label cannot be found in the OCR at all.
    """
    import unicodedata

    def _norm(s: str) -> str:
        s = s.lower()
        s = unicodedata.normalize("NFD", s)
        return "".join(c for c in s if unicodedata.category(c) != "Mn")

    if ocr.is_empty():
        return None

    # Build normalized search terms: label + all aliases (min length 3 to avoid noise)
    raw_terms = [field_meta.get("label", "")] + list(field_meta.get("aliases", []))
    search_terms = [_norm(t) for t in raw_terms if t and len(t.strip()) >= 3]
    if not search_terms:
        return None

    # Find the OCR line that best matches any search term
    best_line: Optional[OcrLine] = None
    best_score = 0.0
    for line in ocr.lines:
        if not line.text.strip():
            continue
        line_norm = _norm(line.text)
        for term in search_terms:
            if term in line_norm:
                # Reward specificity: term that fills more of the line is a tighter match
                score = len(term) / max(len(line_norm), 1)
                if score > best_score:
                    best_score = score
                    best_line = line

    # Require at least 15% coverage to avoid spurious matches on very short aliases
    if best_line is None or best_score < 0.15:
        return None

    x1, y1, x2, y2 = best_line.bbox
    label_h = max(y2 - y1, 1)
    pad_x = max(8, int(ocr.image_width * 0.015))
    pad_y = max(4, int(ocr.image_height * 0.008))

    # ── Pattern 1: inline value after colon ──────────────────────────────
    if ":" in best_line.text:
        after_colon = best_line.text.split(":", 1)[1].strip()
        if after_colon:
            crop = (
                max(0, x1 - pad_x), max(0, y1 - pad_y),
                min(ocr.image_width, x2 + pad_x), min(ocr.image_height, y2 + pad_y),
            )
            return FieldLocalization(
                label_line=best_line,
                value_text=after_colon,
                value_bbox=crop,
                value_confidence=best_line.confidence,
            )

    # ── Pattern 2: value on the next line(s) with horizontal overlap ─────
    candidates = []
    for line in ocr.lines:
        lx1, ly1, lx2, ly2 = line.bbox
        if ly1 <= y2:
            continue                              # must be below label
        if ly1 > y2 + label_h * 2.5:
            continue                              # too far below
        overlap = min(lx2, x2) - max(lx1, x1)
        if overlap < (x2 - x1) * 0.2:
            continue                              # <20% horizontal overlap → unrelated
        candidates.append((ly1, line))

    if candidates:
        _, val_line = min(candidates)
        vx1, vy1, vx2, vy2 = val_line.bbox
        crop = (
            max(0, min(x1, vx1) - pad_x), max(0, y1 - pad_y),
            min(ocr.image_width, max(x2, vx2) + pad_x), min(ocr.image_height, vy2 + pad_y),
        )
        return FieldLocalization(
            label_line=best_line,
            value_text=val_line.text if val_line.text.strip() else None,
            value_bbox=crop,
            value_confidence=val_line.confidence if val_line.text.strip() else 0.0,
        )

    # ── Pattern 3: label found, value area blank/handwritten ────────────
    # Extend bbox downward and to the right to capture the likely fill area.
    crop = (
        max(0, x1 - pad_x), max(0, y1 - pad_y),
        min(ocr.image_width, x2 + int((x2 - x1) * 1.5) + pad_x),
        min(ocr.image_height, y2 + int(label_h * 3) + pad_y),
    )
    return FieldLocalization(
        label_line=best_line,
        value_text=None,
        value_bbox=crop,
        value_confidence=0.0,
    )

def _union_bboxes(
    bboxes: list,
    img_w: int,
    img_h: int,
    padding_pct: float = 0.02,
) -> tuple:
    """Union of multiple bboxes with proportional padding, clamped to image bounds."""
    x1 = min(b[0] for b in bboxes)
    y1 = min(b[1] for b in bboxes)
    x2 = max(b[2] for b in bboxes)
    y2 = max(b[3] for b in bboxes)
    px = int(img_w * padding_pct)
    py = int(img_h * padding_pct)
    return (max(0, x1 - px), max(0, y1 - py), min(img_w, x2 + px), min(img_h, y2 + py))

def _save_image_crop(src_path: str, bbox: tuple, output_dir: str, name: str) -> str:
    """Crop src_path to bbox and save as JPEG under output_dir/smart_crops/."""
    crops_dir = Path(output_dir) / "smart_crops"
    crops_dir.mkdir(parents=True, exist_ok=True)
    img = Image.open(src_path)
    cropped = img.crop(bbox)
    out = crops_dir / f"{name}.jpg"
    cropped.save(str(out), "JPEG", quality=92)
    return str(out)
