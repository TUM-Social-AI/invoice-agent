"""
Build `trace.json` (schema_version 1) from a finished run.

The trace is the contract between the agent and every renderer: it holds the document,
its pages, every field and every compliance finding, each with grounded evidence.
"""

from __future__ import annotations

import hashlib
import math
import re
import time
from pathlib import Path
from typing import Any, Callable, Optional

from src.trace.grounding import (
    STRONG_QUALITIES,
    Grounded,
    PageText,
    find_label,
    ground_field,
    ground_visual,
)
from src.trace.ocr_cache import load_page_ocr_record

SCHEMA_VERSION = 1

# Inventory categories where a missing item of a rule region is expected.
_REGION_CATEGORIES = {
    "header": ("INVOICE_HEADER",),
    "totals": ("TOTALS", "INVOICE_HEADER"),
    "line_items": ("LINE_ITEMS", "INVOICE_HEADER"),
}
# Band of the page (top, bottom as fractions) for the regions precise enough to mark.
_REGION_BANDS = {"header": (0.0, 0.3), "totals": (0.55, 1.0)}

_SEVERITY_ORDER = {"failed": 0, "warning": 1, "not_evaluated": 2, "passed": 3}


def _json_safe(v: Any) -> Any:
    """NaN / inf are not valid JSON; report them as no value."""
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    return v


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def load_pages(state: Any) -> dict[int, PageText]:
    pages: dict[int, PageText] = {}
    for p in range(1, int(state.page_count or len(state.page_image_paths) or 0) + 1):
        rec = load_page_ocr_record(state, p)
        if rec:
            pages[p] = PageText.from_record(rec)
    return pages


def _finding_status(rule_result: Any, trace: Optional[dict]) -> str:
    if trace and trace.get("verdict_missing"):
        return "not_evaluated"
    if rule_result.status == "passed":
        return "passed"
    if rule_result.status == "failed":
        return "failed" if rule_result.severity == "error" else "warning"
    return "not_evaluated"


def _rule_field_names(rule: Any, field_names_by_id: dict[str, str], schema: dict) -> list[str]:
    names: list[str] = []
    fn = field_names_by_id.get(rule.field_id)
    if fn:
        names.append(fn)
    if rule.check_type in ("cross_field", "conditional_check", "required_one_of") and rule.check_value:
        for ident in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", rule.check_value):
            if ident in schema and ident not in names:
                names.append(ident)
            elif ident in field_names_by_id and field_names_by_id[ident] not in names:
                names.append(field_names_by_id[ident])
    return names


def _expected_for_missing(
    rule: Any,
    involved: list[str],
    schema: dict,
    pages: dict[int, PageText],
    inventory: dict[int, str],
    anchor_page: Optional[int],
) -> Optional[dict]:
    """Where a missing item should have been: the field's empty label area, else its page/region."""
    for name in involved:
        meta = schema.get(name)
        if meta:
            g = find_label(meta, pages, prefer_page=anchor_page)
            if g is not None:
                return g.to_dict()
    region = (getattr(rule, "page_region", "") or "").strip().lower()
    page = anchor_page
    for cat in _REGION_CATEGORIES.get(region, ()):  # categories in priority order
        hit = next((p for p, c in sorted(inventory.items()) if c == cat), None)
        if hit is not None:
            page = hit
            break
    if page is None:
        return None
    band = _REGION_BANDS.get(region)
    if band:
        return Grounded(
            page=page, quality="anchor", boxes=[[0.02, band[0] + 0.01, 0.98, band[1] - 0.01]],
            note=f"expected in the {region} area",
        ).to_dict()
    return Grounded(page=page, quality="page", note="expected on this page").to_dict()


def build_trace(
    state: Any,
    store: Any,
    config: dict,
    ocr_missing_pages: Optional[Callable[[list[int]], None]] = None,
) -> dict:
    """
    Assemble the trace. `ocr_missing_pages(pages)` may OCR uncached pages so values the
    agent recorded on the wrong page can still be located; it is called at most once.
    """
    type_id = state.invoice_type_id or ""
    schema = store.build_extraction_schema(type_id) if type_id else {}
    fields_cfg = store.get_fields(type_id) if type_id else []
    field_names_by_id = {f.field_id: f.field_name for f in fields_cfg}
    threshold = float(getattr(state, "confidence_threshold", 0.65))
    inventory = {int(e.get("page", 0)): str(e.get("category", "")).upper() for e in state.page_inventory or []}

    pages = load_pages(state)

    def ground_all() -> dict[str, Grounded]:
        return {
            name: ground_field(fr, schema.get(name, {}), pages)
            for name, fr in state.extracted_fields.items()
        }

    grounded = ground_all()
    weak = [n for n, g in grounded.items() if g.quality not in STRONG_QUALITIES and g.quality != "none"]
    uncached = [p for p in range(1, int(state.page_count or 0) + 1) if p not in pages]
    if weak and uncached and ocr_missing_pages is not None:
        ocr_missing_pages(uncached)
        pages = load_pages(state)
        grounded = ground_all()

    # ── fields ───────────────────────────────────────────────────────────────
    fields_out = []
    for name, fr in state.extracted_fields.items():
        meta = schema.get(name, {})
        low = fr.extracted_value is not None and fr.confidence < threshold
        fields_out.append({
            "field_id": fr.field_id,
            "field_name": name,
            "label": meta.get("label", name),
            "region": meta.get("region", ""),
            "required": bool(meta.get("required")),
            "value": _json_safe(fr.extracted_value),
            "confidence": round(float(_json_safe(fr.confidence) or 0.0), 3),
            "source_page": fr.source_page,
            "source_region": fr.source_region,
            "flagged_for_review": bool(fr.flagged_for_review),
            "review_reason": fr.review_reason,
            "needs_review": bool(fr.flagged_for_review or low or fr.batch_review),
            "low_confidence": low,
            "evidence": grounded[name].to_dict(),
        })
    present = {f["field_name"] for f in fields_out if f["value"] not in (None, "")}
    for name, meta in schema.items():
        if name not in state.extracted_fields:
            fields_out.append({
                "field_id": meta.get("field_id", name), "field_name": name, "label": meta.get("label", name),
                "region": meta.get("region", ""), "required": bool(meta.get("required")), "value": None,
                "confidence": 0.0, "source_page": None, "source_region": None, "flagged_for_review": False,
                "review_reason": None, "needs_review": bool(meta.get("required")), "low_confidence": False,
                "evidence": Grounded(page=None, quality="none", note="not extracted").to_dict(),
            })
    fields_by_name = {f["field_name"]: f for f in fields_out}

    # ── findings ─────────────────────────────────────────────────────────────
    rules = {r.rule_id: r for r in (store.get_rules(type_id) if type_id else [])}
    findings = []
    for rr in state.rule_results:
        rule = rules.get(rr.rule_id)
        rev = state.rule_evidence.get(rr.rule_id, {}) or {}
        trace = rev.get("trace")
        status = _finding_status(rr, trace)
        is_visual = bool(rule and rule.check_type == "visual_check")
        involved = _rule_field_names(rule, field_names_by_id, schema) if rule and not is_visual else []
        evidence: list[dict] = []
        expected = None
        if is_visual and trace:
            evidence = [g.to_dict() for g in ground_visual(trace, pages)]
            anchor_page = trace.get("page_num")
            if not evidence and status in ("failed", "warning"):
                expected = _expected_for_missing(rule, [], schema, pages, inventory, anchor_page)
        else:
            for name in involved:
                ev = fields_by_name.get(name, {}).get("evidence")
                # Only located values become rule evidence; ambiguous candidates stay on the field layer.
                if ev and ev.get("quality") in STRONG_QUALITIES:
                    if all(ev.get("boxes") != e.get("boxes") for e in evidence):
                        evidence.append({**ev, "field_name": name})
            if status in ("failed", "warning", "not_evaluated") and rule is not None:
                missing = [n for n in involved if n not in present]
                if missing:
                    first_page = next((e["page"] for e in evidence if e.get("page")), None)
                    expected = _expected_for_missing(rule, missing, schema, pages, inventory, first_page)
        page = next((e["page"] for e in evidence if e.get("page")), None)
        if page is None and expected:
            page = expected.get("page")
        if page is None and trace:
            page = trace.get("page_num")
        findings.append({
            "rule_id": rr.rule_id,
            "rule_name": rr.rule_name,
            "check_type": rule.check_type if rule else "",
            "status": status,
            "severity": rr.severity,
            "message": rr.message,
            "error_message": getattr(rule, "error_message", "") if rule else "",
            "requirement": (rule.check_value or rule.rule_name) if rule else rr.rule_name,
            "agent_notes": rr.agent_notes,
            "field_names": involved,
            "visual": {
                "evidence_kind": trace.get("evidence_kind"),
                "confidence": trace.get("confidence"),
                "pages_sent": trace.get("pages_sent"),
                "verdict_missing": trace.get("verdict_missing"),
            } if trace else None,
            "page": page,
            "evidence": evidence,
            "expected": expected,
        })
    findings.sort(key=lambda f: (_SEVERITY_ORDER.get(f["status"], 9), f["severity"] != "error", f["rule_id"]))
    for i, f in enumerate(findings, start=1):
        f["ref"] = f"F{i}"

    # ── pages, document, run ─────────────────────────────────────────────────
    finding_pages: dict[int, int] = {}
    for f in findings:
        if f["status"] != "passed" and f["page"]:
            finding_pages[f["page"]] = finding_pages.get(f["page"], 0) + 1
    pages_out = []
    for p in range(1, int(state.page_count or 0) + 1):
        inv = next((e for e in state.page_inventory or [] if int(e.get("page", 0)) == p), {})
        pages_out.append({
            "page": p,
            "category": inv.get("category", ""),
            "description": inv.get("description", ""),
            "ocr": p in pages,
            "findings": finding_pages.get(p, 0),
            "rotation_applied": int((getattr(state, "page_rotation", None) or {}).get(p, 0)),
            # Where text is on the page, so renderers can keep labels off it.
            "text_boxes": [
                [round(v, 4) for v in pages[p].norm_box(l["bbox"], pad=0)]
                for l in pages[p].lines if l.get("text", "").strip()
            ] if p in pages else [],
        })

    inv_type = store.get_type(type_id) if type_id else None
    counts: dict[str, int] = {}
    for f in findings:
        counts[f["status"]] = counts.get(f["status"], 0) + 1
    provenance = getattr(state, "source_provenance", None)
    sha = (getattr(provenance, "content_sha256", None) or "") if provenance else ""
    llm = config.get("llm", {}) or {}
    provider = llm.get("provider", "")
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "run": {
            "run_id": state.run_id,
            "status": state.status.value,
            "finish_reason": state.finish_reason,
            "turns": state.turn,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(state.started_at)),
            "provider": provider,
            "vision_model": (config.get(provider, {}) or {}).get("vision_model", "") if provider else "",
        },
        "document": {
            "file": Path(state.pdf_path).name,
            "path": state.pdf_path,
            "sha256": sha or _sha256(state.pdf_path),
            "page_count": int(state.page_count or 0),
            "invoice_type_id": type_id,
            "invoice_type_name": inv_type.display_name if inv_type else "",
        },
        "pages": pages_out,
        "fields": fields_out,
        "findings": findings,
        "summary": {
            "counts": counts,
            "general_fields": [f["field_name"] for f in fields_out if f["region"] in ("header", "totals")],
            "fields_located": sum(1 for f in fields_out if f["evidence"]["quality"] in STRONG_QUALITIES),
            "fields_with_value": sum(1 for f in fields_out if f["value"] not in (None, "")),
        },
    }
