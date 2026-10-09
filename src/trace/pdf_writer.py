"""
Render the flagged PDF from a trace: summary pages first, then the original pages with
an extraction layer (labeled boxes) and a compliance layer (boxes + PDF comments).

Boxes in the trace are fractions of the upright (displayed) page. They are mapped to PDF
user space through the page's CropBox and total rotation, because pdfium renders the
CropBox and applies /Rotate before OCR ever sees the page.
"""

from __future__ import annotations

import io
import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Optional

from pypdf import PdfReader, PdfWriter
from pypdf.annotations import Link, Text
from pypdf.generic import ArrayObject, FloatObject, NameObject, NumberObject, TextStringObject, Fit
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import simpleSplit
from reportlab.pdfgen import canvas

logger = logging.getLogger(__name__)

# Colours (RGB 0-1)
BLUE = (0.10, 0.35, 0.85)
ORANGE = (0.93, 0.55, 0.00)
RED = (0.84, 0.10, 0.10)
GREY = (0.45, 0.45, 0.45)
GREEN = (0.10, 0.55, 0.25)
INK = (0.10, 0.10, 0.12)
QUIET = (0.40, 0.40, 0.45)

STATUS_COLOR = {"failed": RED, "warning": ORANGE, "not_evaluated": GREY, "passed": GREEN}
STATUS_LABEL = {"failed": "FAILED", "warning": "WARNING", "not_evaluated": "NOT EVALUATED", "passed": "PASSED"}
QUALITY_LABEL = {
    "confirmed": "located (confirmed by OCR)",
    "matched": "located (OCR match)",
    "cited": "located (cited by model)",
    "cited_unverified": "cited by model, not confirmed by OCR",
    "ambiguous": "several candidates",
    "anchor": "label area only",
    "page": "page only",
    "none": "-",
}
STRONG = ("confirmed", "matched")
_COMMENT_FLAGS = 4 | 8 | 16  # Print | NoZoom | NoRotate: icon stays upright and readable


def _txt(s: Any, limit: int = 0) -> str:
    """Text safe for the standard (WinAnsi) PDF fonts."""
    t = unicodedata.normalize("NFC", str("" if s is None else s)).replace("\n", " ").replace("\r", " ")
    t = t.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    t = t.replace("–", "-").replace("—", "-").replace("…", "...")
    t = t.encode("cp1252", "replace").decode("cp1252")
    if limit and len(t) > limit:
        t = t[: max(0, limit - 3)] + "..."
    return t


def _plain(s: Any) -> str:
    """Annotation text: PDF text strings are Unicode, so only control characters are removed."""
    return "".join(ch for ch in str("" if s is None else s) if ch == "\n" or unicodedata.category(ch)[0] != "C")


def _breakable(s: str, width: int = 28) -> str:
    """Insert spaces into very long tokens (IBANs, URLs) so table cells can wrap them."""
    return re.sub(r"\S{%d,}" % (width + 1), lambda m: " ".join(m.group(0)[i:i + width] for i in range(0, len(m.group(0)), width)), s)


# ── page geometry ─────────────────────────────────────────────────────────────

@dataclass
class PageGeom:
    x0: float
    y0: float
    w: float  # unrotated CropBox width
    h: float
    rot: int  # total clockwise rotation shown by the viewer

    @classmethod
    def of(cls, page: Any) -> "PageGeom":
        cb = page.cropbox
        return cls(float(cb.left), float(cb.bottom), float(cb.width), float(cb.height), int(page.rotation or 0) % 360)

    @property
    def disp_w(self) -> float:
        return self.h if self.rot in (90, 270) else self.w

    @property
    def disp_h(self) -> float:
        return self.w if self.rot in (90, 270) else self.h

    def user(self, nx: float, ny: float) -> tuple[float, float]:
        """Displayed fraction (top-left origin) → PDF user space."""
        if self.rot == 90:
            return self.x0 + ny * self.w, self.y0 + nx * self.h
        if self.rot == 180:
            return self.x0 + (1 - nx) * self.w, self.y0 + ny * self.h
        if self.rot == 270:
            return self.x0 + (1 - ny) * self.w, self.y0 + (1 - nx) * self.h
        return self.x0 + nx * self.w, self.y0 + (1 - ny) * self.h

    def user_pt(self, dx: float, dy: float) -> tuple[float, float]:
        """Displayed points (top-left origin) → PDF user space."""
        return self.user(dx / self.disp_w, dy / self.disp_h)

    def user_rect(self, nbox: list[float]) -> tuple[float, float, float, float]:
        ax, ay = self.user(nbox[0], nbox[1])
        bx, by = self.user(nbox[2], nbox[3])
        return min(ax, bx), min(ay, by), max(ax, bx), max(ay, by)

    def user_rect_pts(self, dx1: float, dy1: float, dx2: float, dy2: float) -> tuple[float, float, float, float]:
        return self.user_rect([dx1 / self.disp_w, dy1 / self.disp_h, dx2 / self.disp_w, dy2 / self.disp_h])


# ── page overlay (boxes + labels) ─────────────────────────────────────────────

@dataclass
class Mark:
    nbox: list[float]
    color: tuple
    label: str
    dashed: bool = False
    width: float = 0.9
    short: str = ""  # fallback label (field name / finding ref) when the full one does not fit
    tag: str = ""    # last resort: tiny tag on the box, full label listed in the margin


MARGIN = 150.0  # points added right of each marked page for comment icons and the label legend


class Overlay:
    """
    Draws the marks of one page. All boxes are known before any label is placed, so labels
    avoid other boxes (the content they frame) as well as other labels. Labels are placed
    in displayed space so they read upright on rotated pages.
    """

    LABEL_SIZE = 5.6
    LEGEND_SIZE = 5.4

    def __init__(self, page: Any, text_boxes: Optional[list[list[float]]] = None):
        self.g = PageGeom.of(page)
        self.text_rects = [self._rect_pts(b) for b in (text_boxes or [])]
        mb = page.mediabox
        self.buf = io.BytesIO()
        self.c = canvas.Canvas(self.buf, pagesize=(float(mb.right), float(mb.top)))
        self.marks: list[Mark] = []
        self.connectors: list[tuple] = []
        self.taken: list[tuple[float, float, float, float]] = []  # labels placed, displayed points
        self.legend: list[tuple[float, str, tuple]] = []  # (dy, text, colour) for the margin
        self.icon_slots: list[float] = []

    # displayed-space helpers
    def _rect_pts(self, nbox: list[float]) -> tuple[float, float, float, float]:
        g = self.g
        return nbox[0] * g.disp_w, nbox[1] * g.disp_h, nbox[2] * g.disp_w, nbox[3] * g.disp_h

    @staticmethod
    def _overlaps(a: tuple, b: tuple, pad: float = 0.5) -> bool:
        return not (a[2] <= b[0] + pad or a[0] >= b[2] - pad or a[3] <= b[1] + pad or a[1] >= b[3] - pad)

    def _free(self, r: tuple, own: tuple, avoid_text: bool) -> bool:
        x1, y1, x2, y2 = r
        if x1 < 0 or y1 < 0 or x2 > self.g.disp_w or y2 > self.g.disp_h:
            return False
        if any(self._overlaps(r, t) for t in self.taken):
            return False
        if avoid_text and any(self._overlaps(r, t, pad=1.0) for t in self.text_rects):
            return False
        return all(not self._overlaps(r, self._rect_pts(m.nbox)) for m in self.marks if self._rect_pts(m.nbox) != own)

    def add(self, m: Mark) -> None:
        self.marks.append(m)

    def connector(self, nbox: list[float], dx_icon: float, dy_icon: float, color: tuple) -> None:
        self.connectors.append((nbox, dx_icon, dy_icon, color))

    def icon_slot(self, dy: float, h: float = 16, gap: float = 4) -> float:
        """Free vertical slot for a comment icon in the margin, near `dy`."""
        dy = max(4.0, dy)
        while any(abs(dy - y) < h + gap for y in self.icon_slots):
            dy += h + gap
        dy = min(dy, self.g.disp_h - h - 4)
        self.icon_slots.append(dy)
        return dy

    # drawing
    def _draw_box(self, m: Mark) -> None:
        c, g = self.c, self.g
        x1, y1, x2, y2 = g.user_rect(m.nbox)
        c.saveState()
        c.setStrokeColorRGB(*m.color)
        c.setLineWidth(m.width)
        c.setDash(2.2, 1.6) if m.dashed else c.setDash()
        c.rect(x1, y1, x2 - x1, y2 - y1, stroke=1, fill=0)
        c.restoreState()

    def _place(self, text: str, own: tuple, size: float, avoid_text: bool = True) -> Optional[tuple]:
        bx1, by1, bx2, by2 = own
        tw = self.c.stringWidth(text, "Helvetica", size) + 3
        th = size + 2
        candidates = [
            (bx1, by1 - th),            # above, left-aligned
            (bx2 - tw, by1 - th),       # above, right-aligned
            (bx1 - tw - 2, by1),        # left
            (bx2 + 2, by1),             # right
            (bx1, by2),                 # below
            (bx2 - tw, by2),            # below, right-aligned
        ]
        for lx, ly in candidates:
            r = (lx, ly, lx + tw, ly + th)
            if self._free(r, own, avoid_text):
                return r
        return None

    def _label(self, m: Mark) -> None:
        if not m.label:
            return
        own = self._rect_pts(m.nbox)
        # Labels never cover other boxes and, if at all possible, no text on the scan.
        for text in dict.fromkeys(t for t in (m.label, m.short) if t):
            r = self._place(text, own, self.LABEL_SIZE)
            if r:
                self._draw_label(text, r, m.color, self.LABEL_SIZE)
                self.taken.append(r)
                return
        # Nothing fits next to the box: a tiny tag, the full label goes to the margin legend.
        tag = m.tag or m.short or m.label
        r = self._place(tag, own, self.LABEL_SIZE) or self._place(tag, own, self.LABEL_SIZE, avoid_text=False)
        if r is None:
            tw = self.c.stringWidth(tag, "Helvetica", self.LABEL_SIZE) + 3
            r = (own[0] + 1, own[1] + 1, own[0] + 1 + tw, own[1] + 1 + self.LABEL_SIZE + 2)  # inside the box corner
        self._draw_label(tag, r, m.color, self.LABEL_SIZE)
        self.taken.append(r)
        if tag != m.label:
            self.legend.append((own[1], f"{tag}  {m.label}", m.color))

    def _draw_label(self, text: str, r: tuple, color: tuple, size: float, bg: float = 0.6) -> None:
        c, g = self.c, self.g
        lx, ly, rx, ry = r
        ax, ay = g.user_pt(lx, ry)  # displayed bottom-left of the label
        c.saveState()
        c.translate(ax, ay)
        c.rotate(g.rot)
        if bg:
            c.setFillColorRGB(1, 1, 1)
            c.setFillAlpha(bg)
            c.rect(0, 0, rx - lx, ry - ly, stroke=0, fill=1)
            c.setFillAlpha(1)
        c.setFillColorRGB(*color)
        c.setFont("Helvetica", size)
        c.drawString(1.5, 2.0, text)
        c.restoreState()

    def _draw_connector(self, nbox: list[float], dx_icon: float, dy_icon: float, color: tuple) -> None:
        g, c = self.g, self.c
        sx, sy = g.user(nbox[2], (nbox[1] + nbox[3]) / 2)
        ex, ey = g.user_pt(dx_icon, dy_icon)
        c.saveState()
        c.setStrokeColorRGB(*color)
        c.setLineWidth(0.4)
        c.setDash(0.8, 1.6)
        c.line(sx, sy, ex, ey)
        c.restoreState()

    def _draw_margin(self) -> None:
        g, c = self.g, self.c
        # Separator between the scan and the margin strip.
        x1, y1 = g.user_pt(g.disp_w + 1, 0)
        x2, y2 = g.user_pt(g.disp_w + 1, g.disp_h)
        c.saveState()
        c.setStrokeColorRGB(0.82, 0.82, 0.85)
        c.setLineWidth(0.5)
        c.line(x1, y1, x2, y2)
        c.restoreState()
        used: list[tuple[float, float]] = []
        lx = g.disp_w + 26
        width = MARGIN - 30
        for dy, text, color in sorted(self.legend):
            lines = simpleSplit(_txt(text), "Helvetica", self.LEGEND_SIZE, width)[:3]
            h = len(lines) * (self.LEGEND_SIZE + 1.5) + 2
            y = max(4.0, dy)
            while any(not (y + h <= a or y >= b) for a, b in used):
                y += 2
            used.append((y, y + h))
            for k, ln in enumerate(lines):
                self._draw_label(ln, (lx, y + k * (self.LEGEND_SIZE + 1.5), lx + width, y + (k + 1) * (self.LEGEND_SIZE + 1.5)), color, self.LEGEND_SIZE, bg=0)

    def page_object(self, mediabox: Any = None) -> Any:
        """The overlay as a page; `mediabox` (the widened target page box) keeps margin content from being clipped."""
        for m in self.marks:
            self._draw_box(m)
        # Compliance labels first (they are added after the field marks but matter most).
        for m in sorted(self.marks, key=lambda m: m.width < 1.0):
            self._label(m)
        for args in self.connectors:
            self._draw_connector(*args)
        if self.marks or self.connectors or self.icon_slots:
            self._draw_margin()
        self.c.showPage()  # an overlay with no marks must still produce a page
        self.c.save()
        page = PdfReader(io.BytesIO(self.buf.getvalue())).pages[0]
        if mediabox is not None:
            from pypdf.generic import RectangleObject

            page.mediabox = RectangleObject([float(v) for v in mediabox])
            page.cropbox = RectangleObject([float(v) for v in mediabox])
        return page


def extend_right_margin(page: Any, g: PageGeom, margin: float = MARGIN) -> None:
    """Widen the page by `margin` points on its displayed right side (scan content untouched)."""
    from pypdf.generic import RectangleObject

    cb, mb = page.cropbox, page.mediabox
    x0, y0, x1, y1 = float(cb.left), float(cb.bottom), float(cb.right), float(cb.top)
    if g.rot == 0:
        x1 += margin
    elif g.rot == 90:
        y1 += margin
    elif g.rot == 180:
        x0 -= margin
    else:  # 270
        y0 -= margin
    page.cropbox = RectangleObject((x0, y0, x1, y1))
    page.mediabox = RectangleObject((
        min(x0, float(mb.left)), min(y0, float(mb.bottom)), max(x1, float(mb.right)), max(y1, float(mb.top)),
    ))


# ── summary pages ─────────────────────────────────────────────────────────────

@dataclass
class LinkTarget:
    summary_page: int
    rect: tuple
    page: int
    nbox: Optional[list[float]]


class Summary:
    W, H = A4
    M = 40

    def __init__(self, title: str):
        self.buf = io.BytesIO()
        self.c = canvas.Canvas(self.buf, pagesize=A4)
        self.c.setTitle(_txt(title))
        self.page = 0
        self.y = self.H - self.M
        self.links: list[LinkTarget] = []

    def need(self, h: float) -> None:
        if self.y - h < self.M + 14:
            self.c.setFont("Helvetica", 7)
            self.c.setFillColorRGB(*QUIET)
            self.c.drawRightString(self.W - self.M, self.M - 8, f"Summary page {self.page + 1}")
            self.c.showPage()
            self.page += 1
            self.y = self.H - self.M

    def heading(self, text: str, size: float = 12) -> None:
        self.need(size + 18)
        self.y -= size + 8
        self.c.setFont("Helvetica-Bold", size)
        self.c.setFillColorRGB(*INK)
        self.c.drawString(self.M, self.y, _txt(text))
        self.y -= 6

    def para(self, text: str, size: float = 8.5, color: tuple = QUIET) -> None:
        lines = simpleSplit(_txt(text), "Helvetica", size, self.W - 2 * self.M)
        for ln in lines:
            self.need(size + 3)
            self.y -= size + 3
            self.c.setFont("Helvetica", size)
            self.c.setFillColorRGB(*color)
            self.c.drawString(self.M, self.y, ln)

    def table(self, cols: list[tuple[str, float]], rows: list[dict], size: float = 7.6) -> None:
        """cols = [(header, width_fraction)]; row = {"cells": [...], "color": rgb?, "target": (page, nbox)?}."""
        avail = self.W - 2 * self.M
        widths = [f * avail for _, f in cols]
        lh = size + 2.4
        self.need(lh + 6)
        self.y -= lh
        self.c.setFont("Helvetica-Bold", size)
        self.c.setFillColorRGB(*QUIET)
        x = self.M
        for (hdr, _), w in zip(cols, widths):
            self.c.drawString(x + 2, self.y, _txt(hdr))
            x += w
        self.y -= 3
        self.c.setStrokeColorRGB(0.8, 0.8, 0.82)
        self.c.setLineWidth(0.5)
        self.c.line(self.M, self.y, self.W - self.M, self.y)
        for row in rows:
            wrapped = [
                simpleSplit(_breakable(_txt(cell)), "Helvetica", size, max(10.0, w - 4)) or [""]
                for cell, w in zip(row["cells"], widths)
            ]
            n = min(4, max(len(w) for w in wrapped))
            h = n * lh + 3
            self.need(h + 2)
            top = self.y
            for i, (lines, w) in enumerate(zip(wrapped, widths)):
                x = self.M + sum(widths[:i])
                for k, ln in enumerate(lines[:n]):
                    color = row.get("colors", {}).get(i, INK)
                    self.c.setFillColorRGB(*color)
                    self.c.setFont("Helvetica-Bold" if i in row.get("bold", ()) else "Helvetica", size)
                    self.c.drawString(x + 2, top - (k + 1) * lh, ln)
            self.y = top - h
            self.c.setStrokeColorRGB(0.9, 0.9, 0.92)
            self.c.line(self.M, self.y, self.W - self.M, self.y)
            tgt = row.get("target")
            if tgt and tgt[0]:
                self.links.append(LinkTarget(self.page, (self.M, self.y, self.W - self.M, top), tgt[0], tgt[1]))

    def finish(self) -> Any:
        self.c.setFont("Helvetica", 7)
        self.c.setFillColorRGB(*QUIET)
        self.c.drawRightString(self.W - self.M, self.M - 8, f"Summary page {self.page + 1}")
        self.c.save()
        return PdfReader(io.BytesIO(self.buf.getvalue()))


def _value_str(v: Any) -> str:
    if v is None or v == "":
        return "not found"
    if isinstance(v, float) and v.is_integer():
        return f"{int(v):,}".replace(",", " ")
    return str(v)


def _first_box(ev: Optional[dict]) -> Optional[list[float]]:
    if ev and ev.get("boxes"):
        return ev["boxes"][0]
    return None


def _finding_target(f: dict) -> tuple[Optional[int], Optional[list[float]]]:
    for ev in f.get("evidence") or []:
        if ev.get("page") and ev.get("boxes"):
            return ev["page"], ev["boxes"][0]
    exp = f.get("expected")
    if exp and exp.get("page"):
        return exp["page"], _first_box(exp)
    return f.get("page"), None


def build_summary(trace: dict) -> Summary:
    doc, run = trace["document"], trace["run"]
    s = Summary(f"Invoice review - {doc['file']}")
    c = s.c
    c.setFont("Helvetica-Bold", 17)
    c.setFillColorRGB(*INK)
    s.y -= 14
    c.drawString(s.M, s.y, "Invoice review")
    s.y -= 16
    c.setFont("Helvetica", 9.5)
    c.drawString(s.M, s.y, _txt(doc["file"], 95))
    status = (run.get("status") or "").upper()
    s.y -= 14
    c.setFont("Helvetica-Bold", 9)
    col = {"PASSED": GREEN, "FAILED": RED, "NEEDS_REVIEW": ORANGE, "ERROR": RED}.get(status, QUIET)
    c.setFillColorRGB(*col)
    c.drawString(s.M, s.y, f"Run status: {status or '-'}")
    c.setFont("Helvetica", 8)
    c.setFillColorRGB(*QUIET)
    c.drawString(s.M + 130, s.y, _txt(
        f"Type: {doc.get('invoice_type_name') or doc.get('invoice_type_id') or '-'}   Pages: {doc['page_count']}   "
        f"Run: {run.get('run_id', '')[:28]}   {run.get('started_at', '')}"
    ))
    s.y -= 4

    fields = trace["fields"]
    by_name = {f["field_name"]: f for f in fields}

    # General info
    s.heading("General information", 11)
    general = [by_name[n] for n in trace["summary"]["general_fields"] if n in by_name]
    rows = []
    for f in general:
        ev = f["evidence"]
        rows.append({
            "cells": [f["label"], _value_str(f["value"]), f"{f['confidence']:.2f}" if f["value"] not in (None, "") else "",
                      f"p{ev['page']}" if ev.get("page") else "", QUALITY_LABEL.get(ev["quality"], ev["quality"])],
            "colors": {1: ORANGE if f["needs_review"] or f["value"] in (None, "") else INK},
            "bold": (0,),
            "target": (ev.get("page"), _first_box(ev)),
        })
    rows.append({"cells": ["Model", f"{run.get('provider', '')} {run.get('vision_model', '')}".strip() or "-", "", "", ""]})
    s.table([("Field", 0.24), ("Value", 0.38), ("Conf.", 0.07), ("Page", 0.07), ("Location", 0.24)], rows)

    # Compliance
    findings = trace["findings"]
    counts = trace["summary"]["counts"]
    s.heading("Compliance", 11)
    s.para(
        f"{counts.get('failed', 0)} failed   {counts.get('warning', 0)} warnings   "
        f"{counts.get('not_evaluated', 0)} not evaluated   {counts.get('passed', 0)} passed. "
        "Click a row to jump to its location in the document.",
        color=INK,
    )
    open_findings = [f for f in findings if f["status"] != "passed"]
    if open_findings:
        rows = []
        for f in open_findings:
            reason = f["error_message"] if f["status"] in ("failed", "warning") and f["error_message"] else f["message"]
            if f["status"] == "not_evaluated" and f.get("visual"):
                if f["visual"].get("skip_reason"):
                    reason = f["visual"]["skip_reason"]
                elif f["visual"].get("verdict_missing"):
                    reason = "The vision model returned no verdict for this rule."
            page, nbox = _finding_target(f)
            loc = "box" if nbox else ("page" if page else "-")
            if f.get("expected") and not f.get("evidence"):
                loc = "expected here" if nbox else "page"
            rows.append({
                "cells": [f["ref"], STATUS_LABEL[f["status"]], f["rule_id"], reason or f["requirement"],
                          f"p{page}" if page else "-", loc],
                "colors": {1: STATUS_COLOR[f["status"]]},
                "bold": (0, 1),
                "target": (page, nbox),
            })
        s.table([("Ref", 0.06), ("Status", 0.12), ("Rule", 0.11), ("Reason", 0.53), ("Page", 0.07), ("Mark", 0.11)], rows)
    passed = [f["rule_id"] for f in findings if f["status"] == "passed"]
    if passed:
        s.para("Passed: " + ", ".join(passed))

    # Extracted fields
    s.heading("Extracted fields", 11)
    rows = []
    for f in sorted(fields, key=lambda x: (x["value"] in (None, ""), not x["needs_review"], x["label"])):
        ev = f["evidence"]
        note = ev.get("note") or ""
        rows.append({
            "cells": [f["label"], _value_str(f["value"]),
                      f"{f['confidence']:.2f}" if f["value"] not in (None, "") else "",
                      f"p{ev['page']}" if ev.get("page") else "",
                      QUALITY_LABEL.get(ev["quality"], ev["quality"]) + (f" - {note}" if note and ev["quality"] not in STRONG else "")],
            "colors": {1: ORANGE if f["needs_review"] else INK},
            "target": (ev.get("page"), _first_box(ev)),
        })
    s.table([("Field", 0.22), ("Value", 0.36), ("Conf.", 0.07), ("Page", 0.07), ("Location", 0.28)], rows)

    # Table of contents
    s.heading("Pages", 11)
    rows = []
    for p in trace["pages"]:
        extra = f"rotated {p['rotation_applied']} deg by agent" if p.get("rotation_applied") else ""
        rows.append({
            "cells": [f"Page {p['page']}", p.get("category") or "-", p.get("description") or "",
                      str(p["findings"]) if p["findings"] else "", extra],
            "target": (p["page"], None),
        })
    s.table([("Page", 0.1), ("Type", 0.17), ("Description", 0.5), ("Findings", 0.09), ("Note", 0.14)], rows)

    s.heading("Legend", 10)
    s.para(
        "Blue box: extracted value, labeled with field, value and confidence. Dashed orange: confidence below "
        "the review threshold. Dashed grey: several possible locations, or only the label area was found. "
        "Red / orange boxes with an F-number: evidence of a failed rule / warning; dashed = where a missing "
        "item was expected. Comment icons in the right margin hold the full finding."
    )
    return s


# ── assembly ──────────────────────────────────────────────────────────────────

def _comment(writer: PdfWriter, page_index: int, rect: tuple, text: str, color: tuple, subject: str) -> None:
    a = Text(rect=rect, text=_plain(text), open=False)
    a[NameObject("/C")] = ArrayObject([FloatObject(v) for v in color])
    a[NameObject("/T")] = TextStringObject("Invoice Agent")
    a[NameObject("/Subj")] = TextStringObject(_plain(subject))
    a[NameObject("/Name")] = NameObject("/Comment")
    a[NameObject("/F")] = NumberObject(_COMMENT_FLAGS)
    writer.add_annotation(page_index, a)


def _finding_comment_text(f: dict) -> str:
    lines = [f"{f['ref']} [{STATUS_LABEL[f['status']]}] {f['rule_id']} ({f['severity']})"]
    if f.get("error_message") and f["status"] in ("failed", "warning"):
        lines.append(f["error_message"])
    if f.get("requirement"):
        lines.append(f"Requirement: {f['requirement']}")
    if f.get("message"):
        lines.append(f"Agent: {f['message']}")
    v = f.get("visual") or {}
    if v:
        if v.get("skip_reason"):
            # The reason itself is already shown as the agent message above.
            lines.append("The run's CSV lists this rule as skipped.")
        elif v.get("verdict_missing"):
            lines.append("The vision model returned no verdict for this rule; the run's CSV lists it as skipped.")
        if v.get("pages_dropped"):
            lines.append("Pages not sent (page cap): " + ", ".join(f"p{p}" for p in v["pages_dropped"]))
        lines.append(f"Evidence kind: {v.get('evidence_kind') or '-'}, confidence {v.get('confidence')}")
    if f.get("field_names"):
        lines.append("Fields: " + ", ".join(f["field_names"]))
    if f.get("expected") and not f.get("evidence"):
        lines.append(f"Marker: {f['expected'].get('note') or 'expected location'}")
    return "\n".join(lines)


def render_flagged_pdf(trace: dict, source_pdf: str, out_path: str, page_rotation: Optional[dict] = None) -> str:
    summary = build_summary(trace)
    summary_reader = summary.finish()
    n_summary = len(summary_reader.pages)

    writer = PdfWriter()
    writer.append(summary_reader)
    writer.append(source_pdf)  # PdfWriter(clone_from=...) crashed on real scans; append works
    for p, angle in (page_rotation or {}).items():
        idx = n_summary + int(p) - 1
        if angle and 0 <= idx < len(writer.pages):
            writer.pages[idx].rotate(int(angle))

    n_doc = len(writer.pages) - n_summary
    fields = trace["fields"]
    findings = [f for f in trace["findings"] if f["status"] != "passed"]

    geoms: dict[int, PageGeom] = {}
    for p in range(1, n_doc + 1):
        page = writer.pages[n_summary + p - 1]
        page_info = next((x for x in trace.get("pages", []) if x.get("page") == p), {})
        ov = Overlay(page, page_info.get("text_boxes"))
        g = ov.g
        geoms[p] = g
        # Extraction layer: E-tags are the fallback when a label does not fit next to its box.
        n_tag = 0
        for f in fields:
            ev = f["evidence"]
            if ev.get("page") != p or not ev.get("boxes"):
                continue
            q = ev["quality"]
            n_tag += 1
            tag = f"E{n_tag}"
            value = _txt(_value_str(f["value"]), 40)
            name = f["field_name"]
            if q in STRONG:
                color = ORANGE if f["needs_review"] else BLUE
                lab = f"{name} = {value} ({f['confidence']:.2f})"
                for k, b in enumerate(ev["boxes"]):
                    if k == 0:
                        ov.add(Mark(b, color, lab, dashed=f["needs_review"], short=name, tag=tag))
                    else:
                        ov.add(Mark(b, color, f"{name} (also)", dashed=f["needs_review"], short=f"{tag}+", tag=f"{tag}+"))
            elif q == "cited_unverified":
                # The model named these lines but their OCR text does not show the value.
                color = ORANGE if f["needs_review"] else BLUE
                lab = f"{name} = {value} ({f['confidence']:.2f}) ?"
                for k, b in enumerate(ev["boxes"]):
                    ov.add(Mark(b, color, lab if k == 0 else "", dashed=True, width=0.7, short=f"{name} ?", tag=f"{tag}?"))
            elif q == "ambiguous":
                for k, b in enumerate(ev["boxes"]):
                    ov.add(Mark(b, GREY, f"{name}? = {value}" if k == 0 else "", dashed=True, width=0.7, short=f"{name}?", tag=f"{tag}?"))
            elif q == "anchor":
                ov.add(Mark(ev["boxes"][0], GREY, f"{name} (label area)", dashed=True, width=0.7, short=name, tag=tag))
        # Compliance layer: boxes on the scan, comment icons in the margin strip.
        for f in findings:
            color = STATUS_COLOR[f["status"]]
            tag = f"{f['ref']} {f['rule_id']}"
            boxes = [b for ev in f.get("evidence") or [] if ev.get("page") == p for b in ev.get("boxes", [])]
            exp = f.get("expected") or {}
            exp_boxes = exp.get("boxes", []) if exp.get("page") == p and not f.get("evidence") else []
            on_page = boxes or exp_boxes or (f.get("page") == p)
            if not on_page:
                continue
            for b in boxes:
                grown = [max(0.0, b[0] - 0.004), max(0.0, b[1] - 0.004), min(1.0, b[2] + 0.004), min(1.0, b[3] + 0.004)]
                ov.add(Mark(grown, color, tag, width=1.2, short=f["ref"], tag=f["ref"]))
            for b in exp_boxes:
                ov.add(Mark(b, color, f"{tag} expected", dashed=True, width=1.0, short=f"{f['ref']} expected", tag=f["ref"]))
            anchor = (boxes or exp_boxes or [None])[0]
            dy = ov.icon_slot((anchor[1] * g.disp_h) if anchor else 24.0)
            dx = g.disp_w + 6
            if anchor:
                ov.connector(anchor, dx, dy + 8, color)
            rect = g.user_rect_pts(dx, dy, dx + 16, dy + 16)
            _comment(writer, n_summary + p - 1, rect, _finding_comment_text(f), color, tag)
        if ov.marks or ov.icon_slots:
            extend_right_margin(page, g)  # geometry `g` still describes the scan area
        page.merge_page(ov.page_object(page.mediabox))

    # Summary links jump to the value's position (/XYZ at the box's displayed top-left).
    for lk in summary.links:
        if not (1 <= lk.page <= n_doc):
            continue
        g = geoms[lk.page]  # geometry of the scan area, before the margin was added
        if lk.nbox:
            nx, ny = max(0.0, lk.nbox[0] - 0.05), max(0.0, lk.nbox[1] - 0.05)
            left, top = g.user(nx, ny)
            fit = Fit.xyz(left=left, top=top, zoom=None)
        else:
            fit = Fit.fit()
        writer.add_annotation(lk.summary_page, Link(rect=lk.rect, target_page_index=n_summary + lk.page - 1, fit=fit))

    writer.add_metadata({"/Title": _txt(f"Flagged: {trace['document']['file']}"), "/Producer": "invoice-agent trace"})
    with open(out_path, "wb") as fh:
        writer.write(fh)
    return out_path
