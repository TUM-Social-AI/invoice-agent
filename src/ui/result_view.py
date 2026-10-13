"""Results view for the browser UI: the flagged PDF as page images plus clickable fields and findings."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from src.trace.pdf_writer import _finding_target, _value_str
from src.ui.rule_summary import failed_reason, field_label, rule_label, skip_reason

# JPEG keeps scans ~5x smaller than PNG; at this quality the boxes and labels stay crisp.
JPEG_QUALITY = 88

# Findings listed in the UI (passed / not evaluated rules have no box worth jumping to).
SHOWN_FINDING_STATUSES = ("failed", "warning")


def _scale_box(box: Optional[list[float]], scan_frac: float) -> Optional[list[float]]:
    """Trace boxes are fractions of the scan; the page image also holds the comment margin."""
    if not box:
        return None
    return [box[0] * scan_frac, box[1], box[2] * scan_frac, box[3]]


def _target(page: Any, box: Optional[list[float]], scan_fracs: dict[int, float]) -> Optional[dict]:
    try:
        p = int(page)
    except (TypeError, ValueError):
        return None
    if p not in scan_fracs:
        return None
    return {"page": p, "box": _scale_box(box, scan_fracs[p])}


def _issue_details(f: dict) -> str:
    """
    What the expanded issue and its pin on the page say, in English. The config's
    error_message is left out on purpose: it is in Spanish (see rule_summary.failed_reason).
    """
    lines = []
    if f.get("check_type") == "visual_check" and f.get("requirement"):
        lines.append(f"Requirement: {f['requirement']}")  # visual requirements are English sentences
    if f.get("field_names"):
        lines.append("Fields: " + ", ".join(field_label(n) for n in f["field_names"]))
    confidence = (f.get("visual") or {}).get("confidence")
    if isinstance(confidence, (int, float)):
        lines.append(f"Model confidence: {confidence:.2f}")
    lines.append(f"Rule {f['rule_id']}")
    return "\n".join(lines)


def build_result_view(trace: dict, run_id: str) -> dict:
    """
    Everything the results panel needs, with boxes already in fractions of each page image
    (top-left origin), so the browser only multiplies by the image size.
    """
    layout = trace["flagged_pdf"]
    pages = []
    scan_fracs: dict[int, float] = {}
    for lp in layout["pages"]:
        full_w = lp["scan_w"] + lp["margin"]
        scan_fracs[lp["page"]] = lp["scan_w"] / full_w
        pages.append({
            "page": lp["page"],
            "image_url": f"/api/runs/{run_id}/pages/{lp['page']}.jpg",
            "aspect": lp["scan_h"] / full_w,
            "comments": [
                {
                    "ref": c["ref"],
                    "rule_id": c["rule_id"],
                    "status": c["status"],
                    "text": c["text"],
                    "box": [c["rect"][0] / full_w, c["rect"][1] / lp["scan_h"],
                            c["rect"][2] / full_w, c["rect"][3] / lp["scan_h"]],
                }
                for c in lp["comments"]
            ],
        })

    fields = []
    for f in trace["fields"]:
        # Hidden for now: the visual compliance check stores entries such as AEXCID_stamp and
        # tax_treatment as extracted fields although they are not fields of the invoice type, so
        # they show up with raw names and no value. They stay in the trace, CSV and PDF summary;
        # drop this filter once the visual check stops writing them into extracted_fields.
        if not f.get("in_schema", True):
            continue
        ev = f.get("evidence") or {}
        has_value = f.get("value") not in (None, "")
        target = None
        if has_value:
            boxes = ev.get("boxes") or []
            target = _target(ev.get("page") or f.get("source_page"), boxes[0] if boxes else None, scan_fracs)
        fields.append({
            "field_name": f["field_name"],
            "label": field_label(f["field_name"]),  # the config's own labels are Spanish
            "value": f.get("value"),
            "display": _value_str(f.get("value")),  # same formatting as the labels in the PDF
            "confidence": f.get("confidence"),
            "needs_review": bool(f.get("needs_review")),
            "review_reason": f.get("review_reason") or "",
            "quality": ev.get("quality") or "",
            "target": target,
        })

    findings = []
    order = {s: i for i, s in enumerate(SHOWN_FINDING_STATUSES)}
    for f in trace["findings"]:
        if f.get("status") not in order:
            continue
        page, box = _finding_target(f)
        findings.append({
            "ref": f["ref"],
            "rule_id": f["rule_id"],
            "status": f["status"],
            "severity": f.get("severity") or "",
            "title": rule_label(f.get("rule_name") or "") or f["rule_id"],
            "reason": failed_reason(f.get("check_type") or "", f.get("message") or ""),
            "comment": _issue_details(f),
            "target": _target(page, box, scan_fracs),
        })
    findings.sort(key=lambda x: order[x["status"]])

    # The pins on the page carry the PDF's comment text; show the same English text as the list,
    # also for rules that could not be checked (those are pinned but not listed).
    by_ref = {f["ref"]: f for f in trace["findings"] if f.get("ref")}
    for p in pages:
        for c in p["comments"]:
            f = by_ref.get(c["ref"])
            if not f:
                continue
            if f.get("status") in order:
                reason = failed_reason(f.get("check_type") or "", f.get("message") or "")
            else:
                reason = skip_reason(f.get("message") or "")
            c["title"] = f"{f['ref']} · {rule_label(f.get('rule_name') or '') or f['rule_id']}"
            c["text"] = "\n".join(t for t in (reason, _issue_details(f)) if t)

    doc, run = trace.get("document") or {}, trace.get("run") or {}
    return {
        "file": doc.get("file") or "",
        "invoice_type": doc.get("invoice_type_name") or doc.get("invoice_type_id") or "",
        "status": run.get("status") or "",
        "pdf_url": f"/api/runs/{run_id}/flagged.pdf",
        "pages": pages,
        "fields": fields,
        "findings": findings,
    }


def edited_field(field: dict, edit: Optional[dict]) -> dict:
    """
    A field as the page shows it once a reviewer corrected it. The AI's reading is kept in
    `edit.original` so the page can show both. The location box stays where the AI read it.
    """
    if not edit:
        return field
    value = edit["value"] or None  # an emptied value reads as "not found"
    return {
        **field,
        "value": value,
        "display": _value_str(value),
        "edit": {"at": edit["at"], "by": edit.get("by") or "reviewer", "original": field["display"]},
    }


def apply_field_edits(view: dict, edits: dict[str, dict]) -> dict:
    if not edits:
        return view
    return {**view, "fields": [edited_field(f, edits.get(f["field_name"])) for f in view["fields"]]}


def render_page_images(flagged_pdf: str, layout: dict, out_dir: str | Path, scale: float) -> dict[int, str]:
    """
    Render each source page of the flagged PDF to JPEG, skipping the summary pages.
    Annotations are not drawn: the UI shows the comment icons itself, so they stay clickable.
    Images already in `out_dir` are kept: each run has its own folder, so they come from this
    same flagged PDF (a saved run reopened after a restart).
    """
    out = Path(out_dir)
    images = {lp["page"]: str(out / f"p{lp['page']}.jpg") for lp in layout["pages"]}
    if all(Path(p).is_file() for p in images.values()):
        return images

    import pypdfium2 as pdfium

    out.mkdir(parents=True, exist_ok=True)
    doc = pdfium.PdfDocument(flagged_pdf)
    try:
        for lp in layout["pages"]:
            path = out / f"p{lp['page']}.jpg"
            page = doc[lp["pdf_index"]]
            try:
                page.render(scale=scale, draw_annots=False).to_pil().convert("RGB").save(path, quality=JPEG_QUALITY)
            finally:
                page.close()
    finally:
        doc.close()
    return images


def load_result(paths: dict[str, str], run_id: str, out_dir: str | Path, scale: float) -> Optional[tuple[dict, dict[int, str]]]:
    """View model + page images for a finished run, or None when it has no flagged PDF layout."""
    trace_path, pdf_path = paths.get("trace_json"), paths.get("flagged_pdf")
    if not trace_path or not pdf_path:
        return None
    trace = json.loads(Path(trace_path).read_text(encoding="utf-8"))
    if not trace.get("flagged_pdf"):
        return None
    images = render_page_images(pdf_path, trace["flagged_pdf"], out_dir, scale)
    return build_result_view(trace, run_id), images
