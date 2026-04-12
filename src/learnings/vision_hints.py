"""
Parse `### vision_model_extraction` sections from learnings markdown.

Those bullets are injected into extract_fields_vision prompts only (not the planning agent).
"""

from __future__ import annotations

import re


_VISION_SECTION = re.compile(
    r"^###\s+vision_model_extraction\s*\n(?P<body>.*?)(?=^###\s+|\n##\s|\Z)",
    re.MULTILINE | re.DOTALL | re.IGNORECASE,
)

# Strip standard learning prefixes from bullet text for the vision prompt.
_LEAD_ID = re.compile(r"^\[L\d+\]\s*(\[\d{4}-\d{2}-\d{2}\]\s*)?", re.IGNORECASE)
_LEAD_INITIAL = re.compile(r"^\[initial\]\s*", re.IGNORECASE)


def vision_model_extraction_bullets(learnings_markdown: str) -> list[str]:
    """
    Return de-duplicated bullet lines from every vision_model_extraction subsection
    (e.g. under GENERAL and under a type section). Order: first occurrence wins.
    """
    if not learnings_markdown or not learnings_markdown.strip():
        return []
    seen: set[str] = set()
    out: list[str] = []
    for m in _VISION_SECTION.finditer(learnings_markdown):
        body = m.group("body") or ""
        for line in body.splitlines():
            line = line.strip()
            if not line.startswith("-"):
                continue
            raw = line[1:].strip()
            raw = _LEAD_ID.sub("", raw)
            raw = _LEAD_INITIAL.sub("", raw)
            raw = raw.strip()
            if not raw:
                continue
            key = raw.casefold()
            if key in seen:
                continue
            seen.add(key)
            out.append(raw)
    return out
