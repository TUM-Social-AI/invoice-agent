"""
Ground extracted values and rule evidence to boxes on the page.

Inputs are the cached surya OCR per page and what the agent recorded during the run
(line IDs the vision model cited, OCR-direct lines, visual verdict citations). The
decision ladder for a field, strongest first:

1. confirmed  — the cited lines contain the value
2. matched    — the value text was found in the OCR (nearest to the field's label when it
                occurs several times; other pages are searched when the recorded page has no match)
3. cited_unverified — the model cited lines whose OCR text does not show the value (OCR misread,
                handwriting, enum values like "cash"); drawn dashed, not counted as located
4. ambiguous  — several matches and nothing to choose between them; all candidates are kept
5. anchor     — only the field's label was found; the box is the area next to it
6. page       — nothing on the page could be tied to the value

Boxes are fractions (0-1) of the page image the OCR ran on, i.e. the upright page.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Optional

from src.compliance.evidence import normalize_date_token
from src.tools.compliance_eval import _normalize_numeric
from src.tools.ocr_layout import OcrLine, OcrResult, _localize_field_in_ocr
from src.trace.evidence import parse_line_id

STRONG_QUALITIES = ("confirmed", "matched")


@dataclass
class PageText:
    page: int
    width: int
    height: int
    lines: list[dict]  # {"text", "confidence", "bbox"} in pixels

    @classmethod
    def from_record(cls, record: dict) -> "PageText":
        return cls(
            page=int(record["page"]),
            width=int(record.get("width") or 1),
            height=int(record.get("height") or 1),
            lines=list(record.get("lines", [])),
        )

    def as_ocr_result(self) -> OcrResult:
        return OcrResult(
            lines=[
                OcrLine(text=l.get("text", ""), confidence=float(l.get("confidence", 0.0)), bbox=tuple(l["bbox"]))
                for l in self.lines
            ],
            image_width=self.width,
            image_height=self.height,
        )

    def norm_box(self, px_box: tuple, pad: float = 0.003) -> list[float]:
        x1, y1, x2, y2 = px_box
        return [
            round(max(0.0, x1 / self.width - pad), 5),
            round(max(0.0, y1 / self.height - pad), 5),
            round(min(1.0, x2 / self.width + pad), 5),
            round(min(1.0, y2 / self.height + pad), 5),
        ]

    def union_px(self, idxs: list[int]) -> tuple:
        bs = [self.lines[i]["bbox"] for i in idxs]
        return (min(b[0] for b in bs), min(b[1] for b in bs), max(b[2] for b in bs), max(b[3] for b in bs))

    def text_of(self, idxs: list[int]) -> str:
        return " ".join(self.lines[i].get("text", "") for i in idxs).strip()


@dataclass
class Grounded:
    page: Optional[int]
    quality: str  # confirmed | matched | cited | ambiguous | anchor | page | none
    boxes: list[list[float]] = field(default_factory=list)
    line_ids: list[str] = field(default_factory=list)
    text: str = ""
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "page": self.page,
            "quality": self.quality,
            "kind": (
                "ocr_lines" if self.quality in STRONG_QUALITIES + ("cited", "cited_unverified", "ambiguous")
                else "label_anchor" if self.quality == "anchor"
                else "page_only" if self.quality == "page"
                else "none"
            ),
            "boxes": self.boxes,
            "line_ids": self.line_ids,
            "text": self.text,
            "note": self.note,
        }


# ── value matching ────────────────────────────────────────────────────────────

def _norm(s: Any) -> str:
    s = unicodedata.normalize("NFD", str(s or "").lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


_NUM_TOKEN = re.compile(r"\d[\d\s.,'’]*\d|\d")
_DATE_DMY = re.compile(r"(?<![\d\-/.])(\d{1,2})\s*[/.\-]\s*(\d{1,2})\s*[/.\-]\s*(\d{4}|\d{2})(?!\d)")
_DATE_ISO = re.compile(r"(\d{4})\s*-\s*(\d{1,2})\s*-\s*(\d{1,2})")


def _digit_keys(n: float) -> set[str]:
    """Integer digits of `n` ("129000"); only used for relaxed matching of cited lines."""
    return {str(int(round(abs(n))))} if abs(n - round(n)) < 0.005 else {re.sub(r"\D", "", f"{abs(n):.2f}")}


# A grouped integer ("500.250", "240 000", "1,190") or a decimal with 2 fraction digits ("1.190,50").
_GROUPED_INT = re.compile(r"^\d{1,3}(?:[.,'’ ]\d{3})+$")
_DECIMAL_2 = re.compile(r"^\d{1,3}(?:[.,'’ ]?\d{3})*[.,]\d{2}$")


def _token_values(tok: str) -> set[float]:
    """Plausible numeric readings of one OCR token (thousands vs decimal separators are ambiguous)."""
    t = tok.strip()
    vals: set[float] = set()
    v = _normalize_numeric(t)
    if v is not None:
        vals.add(round(v, 2))
    if _GROUPED_INT.match(t):
        vals.add(float(re.sub(r"\D", "", t)))
    if _DECIMAL_2.match(t):
        vals.add(round(int(re.sub(r"\D", "", t)) / 100, 2))
    return vals


def _numbers_match(n: float, text: str, relaxed: bool = False) -> bool:
    target = round(float(n), 2)
    keys = _digit_keys(n)
    for tok in _NUM_TOKEN.findall(text):
        # A token may glue neighbouring numbers ("10 2500"); try the whole token and its parts.
        for part in dict.fromkeys([tok.strip()] + tok.split()):
            if any(abs(v - target) < 0.005 for v in _token_values(part)):
                return True
            if relaxed:
                # OCR noise on a line the model cited: "129 0001" for "129 000F".
                d = re.sub(r"\D", "", part)
                if any(len(k) >= 4 and d.startswith(k) and len(d) - len(k) <= 1 for k in keys):
                    return True
    return False


_MONTHS = {
    "jan": 1, "janv": 1, "janvier": 1, "enero": 1, "january": 1, "ene": 1,
    "feb": 2, "fev": 2, "fevr": 2, "fevrier": 2, "febrero": 2, "february": 2,
    "mar": 3, "mars": 3, "marzo": 3, "march": 3,
    "apr": 4, "avr": 4, "avril": 4, "abril": 4, "april": 4, "abr": 4,
    "may": 5, "mai": 5, "mayo": 5,
    "jun": 6, "juin": 6, "junio": 6, "june": 6,
    "jul": 7, "juil": 7, "juillet": 7, "julio": 7, "july": 7,
    "aug": 8, "aout": 8, "agosto": 8, "august": 8, "ago": 8,
    "sep": 9, "sept": 9, "septembre": 9, "septiembre": 9, "september": 9,
    "oct": 10, "octobre": 10, "octubre": 10, "october": 10,
    "nov": 11, "novembre": 11, "noviembre": 11, "november": 11,
    "dec": 12, "decembre": 12, "diciembre": 12, "december": 12, "dic": 12,
}
_DATE_WORDS = re.compile(r"(\d{1,2})\s*(?:er|de)?\s+([a-z]+)\.?\s+(?:de\s+)?(\d{4})")


def _dates_in(text: str) -> set[str]:
    out = set()
    for d, mon, y in _DATE_WORDS.findall(_norm(text)):
        m = _MONTHS.get(mon)
        if m:
            out.add(f"{y}-{m:02d}-{int(d):02d}")
    for d, m, y in _DATE_DMY.findall(text):
        if len(y) == 2:
            y = "20" + y
        out.add(f"{y}-{m.zfill(2)}-{d.zfill(2)}")
    for y, m, d in _DATE_ISO.findall(text):
        out.add(f"{y}-{m.zfill(2)}-{d.zfill(2)}")
    return out


def value_kind(value: Any, ftype: str) -> str:
    s = str(value).strip()
    if ftype == "date" or re.fullmatch(r"\d{4}-\d{2}-\d{2}", normalize_date_token(s)):
        return "date"
    if ftype == "decimal" or isinstance(value, (int, float)) or re.fullmatch(r"[\d\s.,'’€$%+-]+", s):
        if _normalize_numeric(s) is not None:
            return "number"
    return "text"


def _value_dates(value: Any) -> set[str]:
    s = str(value).strip()
    iso = normalize_date_token(s)
    out = _dates_in(s)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", iso):
        out.add(iso)
    return out


def text_matches(value: Any, ftype: str, text: str, relaxed: bool = False) -> bool:
    """
    True when OCR `text` plausibly contains `value`. `relaxed` is for lines the model
    already cited: OCR noise around digits ("129 0001" for "129 000F") is tolerated.
    """
    kind = value_kind(value, ftype)
    if kind == "number":
        n = _normalize_numeric(value)
        if n is None:
            return False
        return _numbers_match(n, text, relaxed=relaxed)
    vdates = _value_dates(value)
    if vdates and vdates & _dates_in(text):
        return True
    if kind == "date":
        return False
    nv, nt = _norm(value), _norm(text)
    if not nv or not nt:
        return False
    if len(nv) >= 3 and nv in nt:
        return True
    if len(nt) >= 4 and nt in nv and len(nt) >= 0.3 * len(nv):
        return True
    return difflib.SequenceMatcher(None, nv, nt).ratio() >= 0.85


def _is_weak_number(value: Any, ftype: str) -> bool:
    """Small numbers (quantities like 10) match too many unrelated lines to be found by search alone."""
    if value_kind(value, ftype) != "number":
        return False
    n = _normalize_numeric(value)
    return n is None or len(str(int(abs(n)))) < 3


def find_matches(value: Any, ftype: str, pt: PageText) -> list[list[int]]:
    """Candidate line groups on a page whose text contains the value (multi-line values grouped)."""
    hits = [i for i, l in enumerate(pt.lines) if l.get("text", "").strip() and text_matches(value, ftype, l["text"])]
    if value_kind(value, ftype) != "text":
        return [[i] for i in hits]
    # Multi-line text values: consecutive lines that each are part of the value form one group.
    nv = _norm(value)
    groups: list[list[int]] = []
    for i in hits:
        if groups and i == groups[-1][-1] + 1 and _norm(pt.lines[i]["text"]) in nv:
            groups[-1].append(i)
        else:
            groups.append([i])
    return groups


# ── field grounding ───────────────────────────────────────────────────────────

# The label must make up a good part of its OCR line; otherwise a long heading that merely
# contains an alias ("PRIMES" for "prime") would be taken as the field's label.
_MIN_LABEL_COVERAGE = 0.35


def _label_loc(meta: dict, pt: PageText):
    if not meta:
        return None
    try:
        loc = _localize_field_in_ocr(meta, pt.as_ocr_result())
    except Exception:
        return None
    if loc is None:
        return None
    line = _norm(loc.label_line.text)
    terms = [_norm(t) for t in [meta.get("label", "")] + list(meta.get("aliases", [])) if t and len(t.strip()) >= 3]
    coverage = max((len(t) / max(len(line), 1) for t in terms if t and t in line), default=0.0)
    return loc if coverage >= _MIN_LABEL_COVERAGE else None


def _nearest_to_label(groups: list[list[int]], pt: PageText, label_bbox: tuple) -> list[int]:
    lx1, ly1, lx2, ly2 = label_bbox

    def dist(g: list[int]) -> float:
        x1, y1, x2, y2 = pt.union_px(g)
        dx = max(0, x1 - lx2, lx1 - x2)
        dy = max(0, y1 - ly2, ly1 - y2)
        # Values sit right of or below their label; penalise candidates above/left.
        penalty = 2.0 if (y2 < ly1 or x2 < lx1) else 1.0
        return (dx + 1.5 * dy) * penalty

    return min(groups, key=dist)


def _valid_cited(field_result: Any, pt: Optional[PageText]) -> list[int]:
    if pt is None:
        return []
    idxs: list[int] = []
    for ev in getattr(field_result, "evidence", []) or []:
        if ev.page is not None and ev.page != pt.page:
            continue
        for lid in ev.line_ids:
            p, idx = parse_line_id(lid)
            if (p is None or p == pt.page) and idx is not None and idx < len(pt.lines) and idx not in idxs:
                idxs.append(idx)
    return sorted(idxs)


def ground_field(field_result: Any, meta: dict, pages: dict[int, PageText]) -> Grounded:
    value = field_result.extracted_value
    page = field_result.source_page
    if value is None or str(value).strip() == "":
        return Grounded(page=page, quality="none", note="no value")
    ftype = (meta.get("type") or meta.get("data_type") or "").strip().lower()
    is_enum = bool(meta.get("enum"))
    pt = pages.get(page) if page else None

    def result(q: str, p: PageText, idxs: list[int], note: str = "") -> Grounded:
        return Grounded(
            page=p.page, quality=q, boxes=[p.norm_box(p.union_px(idxs))],
            line_ids=[f"L{i}" for i in idxs], text=p.text_of(idxs), note=note,
        )

    def result_clusters(q: str, p: PageText, clusters: list[list[int]], note: str = "") -> Grounded:
        clusters = clusters[:3]
        return Grounded(
            page=p.page, quality=q, boxes=[p.norm_box(p.union_px(c)) for c in clusters],
            line_ids=[f"L{i}" for c in clusters for i in c],
            text=" | ".join(p.text_of(c) for c in clusters), note=note,
        )

    cited = _valid_cited(field_result, pt)
    if cited:
        # Cited lines far apart (a line amount and the total) become separate boxes.
        clusters = cluster_lines(pt, cited)
        if is_enum:
            return result_clusters("cited_unverified", pt, clusters, "enum value; the lines the model cited")
        # Lines that hold the value themselves give the tightest box ("62 500", not the amount in words below).
        line_hits = [i for i in cited if text_matches(value, ftype, pt.lines[i].get("text", ""), relaxed=True)]
        if line_hits:
            return result_clusters("confirmed", pt, cluster_lines(pt, line_hits))
        # Multi-line values (descriptions): no single line holds the value, the cited group does.
        hits = [c for c in clusters if text_matches(value, ftype, pt.text_of(c), relaxed=True)]
        if hits:
            return result_clusters("confirmed", pt, hits)
        if text_matches(value, ftype, pt.text_of(cited), relaxed=True):
            return result_clusters("confirmed", pt, clusters)

    label = _label_loc(meta, pt) if pt else None
    if pt is not None and not is_enum:
        groups = find_matches(value, ftype, pt)
        if _is_weak_number(value, ftype) and not label:
            groups = []
        if len(groups) == 1:
            return result("matched", pt, groups[0])
        if len(groups) > 1:
            if label is not None:
                return result("matched", pt, _nearest_to_label(groups, pt, label.label_line.bbox), "nearest to its label")
            if cited:
                inter = [g for g in groups if set(g) & set(cited)]
                if len(inter) == 1:
                    return result("matched", pt, inter[0], "match that the model cited")
            if not cited:
                g = groups[:3]
                return Grounded(
                    page=pt.page, quality="ambiguous",
                    boxes=[pt.norm_box(pt.union_px(x)) for x in g],
                    line_ids=[f"L{i}" for x in g for i in x],
                    text=" | ".join(pt.text_of(x) for x in g),
                    note=f"value appears {len(groups)} times on the page",
                )

    if cited:
        return result_clusters("cited_unverified", pt, cluster_lines(pt, cited), "OCR text differs from the extracted value")

    # The agent sometimes records the wrong page: look for a unique match elsewhere.
    if not is_enum and not _is_weak_number(value, ftype):
        elsewhere = [(p, g) for p, ptx in sorted(pages.items()) if p != page for g in find_matches(value, ftype, ptx)]
        if len(elsewhere) == 1:
            p, g = elsewhere[0]
            return result("matched", pages[p], g, f"found on page {p}; the agent recorded page {page}")

    if pt is not None and label is not None:
        return Grounded(
            page=pt.page, quality="anchor", boxes=[pt.norm_box(label.value_bbox, pad=0)],
            line_ids=[], text=label.label_line.text, note="value not found in OCR; box marks its label area",
        )
    return Grounded(page=page, quality="page", note="value not found in OCR text of the page")


# ── rule evidence ─────────────────────────────────────────────────────────────

def cluster_lines(pt: PageText, idxs: list[int]) -> list[list[int]]:
    """Group cited lines into nearby clusters so one box does not span half the page."""
    clusters: list[list[int]] = []
    for i in sorted(idxs, key=lambda k: (pt.lines[k]["bbox"][1], pt.lines[k]["bbox"][0])):
        b = pt.lines[i]["bbox"]
        h = max(1, b[3] - b[1])
        placed = False
        for c in clusters:
            x1, y1, x2, y2 = pt.union_px(c)
            if b[1] <= y2 + 2.0 * h and b[3] >= y1 - 2.0 * h and b[0] <= x2 + 4 * h and b[2] >= x1 - 4 * h:
                c.append(i)
                placed = True
                break
        if not placed:
            clusters.append([i])
    return clusters


def ground_visual(trace: dict, pages: dict[int, PageText]) -> list[Grounded]:
    """Boxes for a visual verdict's cited lines; anchors extend toward the unlabeled item."""
    by_page: dict[int, list[int]] = {}
    for lid in trace.get("line_ids", []):
        p, idx = parse_line_id(lid)
        if p in pages and idx is not None and idx < len(pages[p].lines):
            by_page.setdefault(p, []).append(idx)
    kind = trace.get("evidence_kind") or "text"
    out: list[Grounded] = []
    for p, idxs in sorted(by_page.items()):
        pt = pages[p]
        for c in cluster_lines(pt, idxs):
            x1, y1, x2, y2 = pt.union_px(c)
            if kind == "anchor":
                # Signature / seal next to its label: include the area below and to the right.
                h = max(1, y2 - y1)
                x2 = min(pt.width, x2 + int((x2 - x1) * 0.5))
                y2 = min(pt.height, y2 + 4 * h)
            out.append(Grounded(
                page=p, quality="cited", boxes=[pt.norm_box((x1, y1, x2, y2))],
                line_ids=[f"p{p}_L{i}" for i in c], text=pt.text_of(c),
            ))
    return out


def find_label(meta: dict, pages: dict[int, PageText], prefer_page: Optional[int] = None) -> Optional[Grounded]:
    """Where a missing field's label sits (its empty value area), searching the preferred page first."""
    order = sorted(pages, key=lambda p: (p != prefer_page, p))
    for p in order:
        loc = _label_loc(meta, pages[p])
        if loc is not None:
            return Grounded(
                page=p, quality="anchor", boxes=[pages[p].norm_box(loc.value_bbox, pad=0)],
                text=loc.label_line.text, note="expected next to this label",
            )
    return None
