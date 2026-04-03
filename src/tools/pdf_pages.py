"""Moved implementations for pdf_pages.py."""

import base64
import ast
import io
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


def inspect_file(state: AgentState) -> dict:
    """
    Read basic metadata about the PDF before doing any heavy processing.
    Tells the agent: filename, size, page count, whether compression is advisable.
    """
    path = Path(state.pdf_path)
    if not path.exists():
        return {"success": False, "error": f"File not found: {state.pdf_path}"}

    size_bytes = path.stat().st_size
    size_mb = round(size_bytes / (1024 * 1024), 2)

    page_count = None
    try:
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(state.pdf_path)
        page_count = len(pdf)
    except Exception as e:
        logger.warning(f"Could not count pages: {e}")

    info = {
        "filename": path.name,
        "size_mb": size_mb,
        "size_bytes": size_bytes,
        "page_count": page_count,
        "format": path.suffix.lower(),
        "suggest_compression": size_mb > 8,
    }
    state.file_info = info
    if page_count:
        state.page_count = page_count

    logger.info(f"File inspection: {path.name} | {size_mb} MB | {page_count} pages")
    return {"success": True, **info}

def compress_pages(state: AgentState, dpi: int = 96, quality: int = 65, max_width: int = 1400) -> dict:
    """
    Re-render PDF pages at lower DPI / quality / size to reduce memory and API load.
    Output goes to tmp_dir/pages/. Updates state.page_image_paths.
    Use when the file is large (>8 MB) or page images are slow to process.
    """
    try:
        import pypdfium2 as pdfium
    except ImportError:
        return {"success": False, "error": "pypdfium2 not installed"}

    pages_dir = Path(state.tmp_dir) / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)

    pdf = pdfium.PdfDocument(state.pdf_path)
    scale = dpi / 72.0
    paths = []

    try:
        for i, page in enumerate(pdf):
            bitmap = page.render(scale=scale, rotation=0)
            img = bitmap.to_pil()
            bitmap.close()
            page.close()

            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")

            # Resize if wider than max_width
            if img.width > max_width:
                ratio = max_width / img.width
                img = img.resize((max_width, int(img.height * ratio)), Image.LANCZOS)

            out_path = pages_dir / f"page_{i+1:03d}.jpg"
            img.save(out_path, "JPEG", quality=quality, optimize=True)
            paths.append(str(out_path))
            logger.debug(f"Compressed page {i+1} → {out_path} (dpi={dpi}, quality={quality})")
    finally:
        pdf.close()

    state.page_image_paths = paths
    state.compressed_page_paths = paths   # kept even after convert_pdf_to_images overwrites page_image_paths
    state.page_count = len(paths)
    state.compressed = True

    return {
        "success": True,
        "page_count": len(paths),
        "page_paths": paths,
        "dpi": dpi,
        "quality": quality,
        "max_width": max_width,
    }

def convert_pdf_to_images(state: AgentState, dpi: int = 150) -> dict:
    """
    Convert all pages of the PDF to images. Stored in output_dir/pages/.
    Lower DPI reduces memory on constrained hardware; 150 is the sweet spot
    for Qwen2-VL quality vs size.
    """
    try:
        import pypdfium2 as pdfium
    except ImportError:
        return {"success": False, "error": "pypdfium2 not installed"}

    pages_dir = Path(state.output_dir) / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)

    pdf = pdfium.PdfDocument(state.pdf_path)
    paths = []
    scale = dpi / 72.0

    try:
        for i, page in enumerate(pdf):
            bitmap = page.render(scale=scale, rotation=0)
            pil_img = bitmap.to_pil()
            bitmap.close()
            page.close()

            # Compress: convert to RGB and save as JPEG to reduce size
            if pil_img.mode in ("RGBA", "P"):
                pil_img = pil_img.convert("RGB")

            out_path = pages_dir / f"page_{i+1:03d}.jpg"
            pil_img.save(out_path, "JPEG", quality=85, optimize=True)
            paths.append(str(out_path))
            logger.debug(f"Rendered page {i+1} → {out_path}")
    finally:
        pdf.close()

    state.page_image_paths = paths
    state.page_count = len(paths)

    return {
        "success": True,
        "page_count": len(paths),
        "page_paths": paths,
        "dpi": dpi,
    }


def render_medium_pages(
    state: AgentState,
    dpi: int = 110,
    quality: int = 78,
    max_width: int = 1800,
) -> dict:
    """
    Render PDF pages at medium DPI/quality into tmp/medium_pages/ for hybrid extraction.
    Does not change state.page_image_paths (full-res stays authoritative).
    """
    try:
        import pypdfium2 as pdfium
    except ImportError:
        return {"success": False, "error": "pypdfium2 not installed"}

    pages_dir = Path(state.tmp_dir) / "medium_pages"
    pages_dir.mkdir(parents=True, exist_ok=True)

    pdf = pdfium.PdfDocument(state.pdf_path)
    paths: list[str] = []
    scale = dpi / 72.0

    try:
        for i, page in enumerate(pdf):
            bitmap = page.render(scale=scale, rotation=0)
            pil_img = bitmap.to_pil()
            bitmap.close()
            page.close()

            if pil_img.mode in ("RGBA", "P"):
                pil_img = pil_img.convert("RGB")

            if pil_img.width > max_width:
                ratio = max_width / pil_img.width
                pil_img = pil_img.resize((max_width, int(pil_img.height * ratio)), Image.LANCZOS)

            out_path = pages_dir / f"page_{i+1:03d}.jpg"
            pil_img.save(out_path, "JPEG", quality=quality, optimize=True)
            paths.append(str(out_path))
    finally:
        pdf.close()

    state.medium_page_paths = paths
    return {
        "success": True,
        "page_count": len(paths),
        "page_paths": paths,
        "dpi": dpi,
        "quality": quality,
        "max_width": max_width,
    }


REGION_CROPS = {
    "header":        (0.0, 0.0,  1.0, 0.25),   # top 25%
    "footer":        (0.0, 0.75, 1.0, 1.0),    # bottom 25%
    "address_block": (0.0, 0.15, 0.55, 0.40),  # left side, upper-mid
    "totals":        (0.4, 0.60, 1.0, 1.0),    # right side, lower
    "line_items":    (0.0, 0.30, 1.0, 0.75),   # middle band
    "body":          (0.0, 0.15, 1.0, 0.85),   # most of page
}

def crop_region(
    state: AgentState,
    image_path: str,
    region: str,
    page_num: int,
    custom_bbox: Optional[tuple] = None,   # (left%, top%, right%, bottom%) relative
) -> dict:
    """
    Crop a specific region from a page image. Returns path to cropped image.
    Uses predefined region fractions or a custom bounding box.
    """
    try:
        img = Image.open(image_path)
        w, h = img.size

        if custom_bbox:
            l, t, r, b = custom_bbox
        else:
            fracs = REGION_CROPS.get(region, (0.0, 0.0, 1.0, 1.0))
            l, t, r, b = fracs

        box = (int(l * w), int(t * h), int(r * w), int(b * h))
        cropped = img.crop(box)

        crops_dir = Path(state.output_dir) / "crops"
        crops_dir.mkdir(parents=True, exist_ok=True)
        key = f"page{page_num}_{region}"
        out_path = crops_dir / f"{key}.jpg"
        cropped.save(out_path, "JPEG", quality=90)

        state.region_crops[key] = str(out_path)
        return {"success": True, "crop_path": str(out_path), "region": region, "key": key}

    except Exception as e:
        return {"success": False, "error": str(e)}

def _image_to_base64(path: str) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def image_to_base64_scaled(
    path: str,
    max_side: int = 1280,
    jpeg_quality: int = 78,
) -> str:
    """
    Resize (if needed) and JPEG-re-encode before base64 for vision API calls.
    Reduces payload size and VRAM for multi-image requests (avoids many Ollama 500s).
    """
    with Image.open(path) as img:
        # JPEG only supports RGB / L; normalize palette + alpha modes (e.g. LA, RGBA, P).
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        w, h = img.size
        longest = max(w, h)
        if longest > max_side:
            scale = max_side / longest
            img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=jpeg_quality, optimize=True)
        return base64.b64encode(buf.getvalue()).decode("utf-8")
