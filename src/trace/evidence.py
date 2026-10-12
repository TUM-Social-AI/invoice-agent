"""
Evidence records and line-ID helpers shared by the agent tools and the trace exporter.

Line IDs are page-scoped (`L12` = line 12 of that page's cached OCR). Prompts that show
several pages at once prefix the page (`p2_L12`).
"""

from __future__ import annotations

import re
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

_LINE_ID_RE = re.compile(r"^(?:p(\d+)_)?L(\d+)$", re.IGNORECASE)


class Evidence(BaseModel):
    """Where a value was read, as recorded during the run (before grounding)."""

    model_config = ConfigDict(extra="forbid")

    page: Optional[int] = None
    line_ids: list[str] = Field(default_factory=list)
    # ocr_direct (deterministic OCR read) | model_cited (vision model named the lines)
    source: str = "model_cited"


def line_id(index: int, page: Optional[int] = None) -> str:
    return f"p{page}_L{index}" if page is not None else f"L{index}"


def parse_line_id(raw: object) -> tuple[Optional[int], Optional[int]]:
    """Return (page or None, line index) for `L12` / `p2_L12` / `[L12]`; (None, None) if invalid."""
    s = str(raw or "").strip().strip("[]").strip()
    m = _LINE_ID_RE.match(s)
    if not m:
        return None, None
    page = int(m.group(1)) if m.group(1) else None
    return page, int(m.group(2))


def coerce_line_ids(raw: object) -> list[str]:
    """Normalize a model-returned citation (list, comma string, single id) to valid `L<n>` / `p<n>_L<n>` ids."""
    if raw is None:
        return []
    items = raw if isinstance(raw, (list, tuple)) else re.split(r"[,\s]+", str(raw))
    out: list[str] = []
    for item in items:
        page, idx = parse_line_id(item)
        if idx is None:
            continue
        lid = line_id(idx, page)
        if lid not in out:
            out.append(lid)
    return out


def restrict_citations(extracted: dict, shown: set[str]) -> None:
    """Drop cited line IDs the model was not shown (in place on a vision extraction payload)."""
    for key in [k for k in extracted if k.endswith("_evidence")]:
        extracted[key] = [lid for lid in coerce_line_ids(extracted[key]) if lid in shown]
