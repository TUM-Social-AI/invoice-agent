"""Moved implementations for ocr_layout.py."""

import base64
import ast
import json
import logging
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import requests
from PIL import Image

from src.agent.state import AgentState, FieldResult, RuleResult
from src.compliance.evidence import required_slots_for_rule, link_pages
from src.config.loader import ConfigStore, ComplianceRule
from src.llm.base import LLMProvider

logger = logging.getLogger(__name__)


@dataclass
class SuryaModels:
    """Holds pre-loaded surya OCR model objects so they are loaded only once at startup."""
    det_predictor: Any
    rec_predictor: Any

def load_surya_models() -> "Optional[SuryaModels]":
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

@dataclass
class OcrLine:
    """Single text line returned by surya, with its pixel-space bounding box."""
    text: str
    confidence: float
    bbox: tuple  # (x1, y1, x2, y2) integers

@dataclass
class OcrResult:
    """Full-page OCR output: structured lines with bboxes + flat text."""
    lines: list  # list[OcrLine]
    image_width: int = 0
    image_height: int = 0

    @property
    def full_text(self) -> str:
        return "\n".join(l.text for l in self.lines if l.text.strip())

    def is_empty(self) -> bool:
        return not self.lines

@dataclass
class FieldLocalization:
    """Result of finding a field's label and value region in the OCR layout."""
    label_line: Any          # OcrLine where the label was found
    value_text: Optional[str]  # Extracted value text, None if blank/handwritten
    value_bbox: tuple        # (x1, y1, x2, y2) crop region to send to vision
    value_confidence: float  # OCR confidence; 0 if value not found/readable

def _ocr_with_layout(
    image_path: str,
    surya_models: "Optional[SuryaModels]",
) -> OcrResult:
    """
    Run surya OCR on a page image and return structured results with bounding boxes.
    Each OcrLine has .text, .confidence, and .bbox (pixel coords).
    Returns empty OcrResult if surya is unavailable or fails.
    """
    if surya_models is None:
        return OcrResult(lines=[])
    try:
        from surya.common.surya.schema import TaskNames
        img = Image.open(image_path)
        w, h = img.size
        predictions = surya_models.rec_predictor(
            images=[img],
            task_names=[TaskNames.ocr_without_boxes],
            det_predictor=surya_models.det_predictor,
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
