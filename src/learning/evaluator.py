"""
Learning mode evaluator.

Loads a ground truth JSON file and compares it against the agent's results.
Returns a structured diff that is passed to the reflection loop in agent.py.

Ground truth file: <pdf_stem>_truth.json alongside the PDF.

Format:
{
  "invoice_type_id": "VIAJES",
  "notes": "optional human annotation about this document",
  "fields": {
    "vendor_name": "Hotel Arts Barcelona",
    "invoice_date": "2024-03-15",
    "total_amount": 245.50,
    "beneficiary": "Ana García López"
  },
  "compliance": {
    "R_VIA_001": "passed",
    "R_VIA_002": "passed",
    "R_VIA_003": "failed"
  }
}
"""

import csv
import json
import logging
import re
from pathlib import Path
from typing import Optional

from src.agent.state import AgentState

logger = logging.getLogger(__name__)

NUMERIC_TOLERANCE = 0.02      # for amounts
RATE_TOLERANCE    = 0.001     # for percentages/rates


def _normalize_number_for_eval(v) -> Optional[float]:
    if v is None:
        return None
    s = str(v).strip()
    if not s or s.lower() in ("null", "none"):
        return None
    s = s.replace(" ", "")
    s = re.sub(r"[€$£%]", "", s)

    # European-style: 1.190,00 -> 1190.00
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            # comma is decimal separator
            s = s.replace(".", "").replace(",", ".")
        else:
            # dot is decimal separator; remove commas (thousands)
            s = s.replace(",", "")
    elif "," in s:
        # single comma likely decimal separator; multiple commas likely thousands
        if s.count(",") == 1:
            s = s.replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "." in s:
        # multiple dots: treat all but last as thousands separators
        if s.count(".") > 1:
            parts = s.split(".")
            s = "".join(parts[:-1]) + "." + parts[-1]

    # Extract first numeric token (handles +/- and stray OCR punctuation)
    m = re.search(r"[+-]?\d[\d\.,]*", s)
    if not m:
        return None
    s = m.group(0)
    # Apply separators heuristics on the extracted numeric token.
    t = s
    if "," in t and "." in t:
        if t.rfind(",") > t.rfind("."):
            # comma is decimal separator; remove '.' thousands
            t = t.replace(".", "").replace(",", ".")
        else:
            # dot is decimal separator; remove ',' thousands
            t = t.replace(",", "")
    elif "," in t:
        if t.count(",") == 1:
            t = t.replace(",", ".")
        else:
            t = t.replace(",", "")
    elif "." in t:
        if t.count(".") > 1:
            parts = t.split(".")
            t = "".join(parts[:-1]) + "." + parts[-1]

    if t in ("", "-", "+"):
        return None

    try:
        return float(t)
    except ValueError:
        return None


def load_ground_truth(pdf_path: str, config: dict | None = None) -> Optional[dict]:
    """
    Find and load the ground truth.

    1) Prefer `<pdf_stem>_truth.json` next to the PDF (existing behavior).
    2) If missing and `agent.ground_truth_csv_path` is configured, load a matching
       row from the CSV and convert it into the expected JSON shape.
    """
    truth_path = Path(pdf_path).with_name(Path(pdf_path).stem + "_truth.json")
    if not truth_path.exists():
        cfg = config.get("agent", {}) if config else {}
        csv_path = cfg.get("ground_truth_csv_path")
        if not csv_path:
            return None

        csv_path = str(csv_path)
        csv_file = Path(csv_path)
        if not csv_file.exists():
            logger.warning(f"Ground truth CSV configured but not found: {csv_file}")
            return None

        source_col = str(cfg.get("ground_truth_source_column", "Source file"))
        col_map: dict = cfg.get("ground_truth_column_map") or {}
        invoice_type_col = cfg.get("ground_truth_invoice_type_column")

        pdf_stem = Path(pdf_path).stem
        pdf_key = re.sub(r"[^a-z0-9]+", "", pdf_stem.lower())

        def _norm_source_key(s: str) -> str:
            stem = Path(str(s).strip()).stem
            return re.sub(r"[^a-z0-9]+", "", stem.lower())

        matched_row = None
        try:
            with open(csv_file, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                if not reader.fieldnames or source_col not in reader.fieldnames:
                    logger.warning(
                        f"Ground truth CSV missing source column '{source_col}'. "
                        f"Found columns: {reader.fieldnames}"
                    )
                    return None
                for row in reader:
                    v = row.get(source_col, "")
                    if not v:
                        continue
                    sk = _norm_source_key(v)
                    if sk == pdf_key or pdf_key in sk or sk in pdf_key:
                        matched_row = row
                        break
        except Exception as e:
            logger.warning(f"Could not read ground truth CSV {csv_file}: {e}")
            return None

        if not matched_row:
            return None

        fields: dict = {}
        for csv_col, field_name in col_map.items():
            if csv_col not in matched_row:
                continue
            raw = matched_row.get(csv_col)
            if raw is None:
                continue
            raw_s = str(raw).strip()
            if not raw_s:
                continue
            # Keep raw strings/numbers; evaluator can normalise numerics/dates.
            fields[str(field_name).strip()] = raw_s

        truth: dict = {"fields": fields}
        if invoice_type_col and invoice_type_col in matched_row:
            truth["invoice_type_id"] = (matched_row.get(invoice_type_col) or "").strip() or None
        return truth

    try:
        return json.loads(truth_path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"Could not load ground truth {truth_path}: {e}")
        return None


# ---------------------------------------------------------------------------
# Value comparison helpers
# ---------------------------------------------------------------------------

def _normalize_number(v) -> Optional[float]:
    # Kept for backward-compatible internal use by compare helpers.
    # Delegates to the more robust normaliser.
    try:
        # Avoid infinite recursion: reuse a slightly different implementation above.
        return float(_normalize_number_for_eval(v)) if _normalize_number_for_eval(v) is not None else None
    except Exception:
        return None


def _normalize_date(v) -> Optional[str]:
    """Try to normalise a date string to YYYY-MM-DD."""
    if v is None:
        return None
    s = str(v).strip()
    # DD/MM/YYYY or DD.MM.YYYY or DD-MM-YYYY
    m = re.match(r"^(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})$", s)
    if m:
        return f"{m.group(3)}-{m.group(2).zfill(2)}-{m.group(1).zfill(2)}"
    # YYYY-MM-DD already
    m = re.match(r"^(\d{4})[/.\-](\d{2})[/.\-](\d{2})$", s)
    if m:
        return s
    return s.lower()


def _normalize_string(v) -> str:
    if v is None:
        return ""
    return str(v).strip().lower()


def _compare_value(extracted, truth_val, field_name: str) -> dict:
    """
    Compare one extracted value against the ground truth.
    Returns {"match": bool, "partial": bool, "extracted": ..., "truth": ...}
    """
    if truth_val is None:
        # No truth provided for this field — skip
        return {"match": None, "partial": False, "extracted": extracted, "truth": truth_val,
                "note": "no ground truth provided for this field"}

    # Null extracted
    if extracted is None or str(extracted).lower() in ("null", "none", ""):
        return {"match": False, "partial": False, "extracted": None, "truth": truth_val,
                "note": "field not extracted"}

    # Try numeric comparison
    ext_num = _normalize_number(extracted)
    tru_num = _normalize_number(truth_val)
    if ext_num is not None and tru_num is not None:
        tol = RATE_TOLERANCE if "rate" in field_name or "pct" in field_name else NUMERIC_TOLERANCE
        ok = abs(ext_num - tru_num) <= tol
        return {"match": ok, "partial": False, "extracted": ext_num, "truth": tru_num,
                "note": f"numeric diff: {abs(ext_num - tru_num):.4f}"}

    # Try date comparison
    if any(kw in field_name for kw in ("date", "fecha", "period", "periodo")):
        ext_d = _normalize_date(extracted)
        tru_d = _normalize_date(truth_val)
        ok = ext_d == tru_d
        return {"match": ok, "partial": False, "extracted": ext_d, "truth": tru_d,
                "note": "" if ok else f"date mismatch: '{ext_d}' vs '{tru_d}'"}

    # String comparison
    ext_s = _normalize_string(extracted)
    tru_s = _normalize_string(truth_val)
    exact = ext_s == tru_s
    partial = (not exact) and (tru_s in ext_s or ext_s in tru_s)
    return {
        "match": exact,
        "partial": partial,
        "extracted": str(extracted),
        "truth": str(truth_val),
        "note": "partial match" if partial else ("" if exact else "mismatch"),
    }


# ---------------------------------------------------------------------------
# Main evaluation function
# ---------------------------------------------------------------------------

def evaluate(state: AgentState, truth: dict) -> dict:
    """
    Compare agent state against ground truth.
    Returns a structured diff ready to be passed to the reflection loop.
    """
    results = {
        "type_match": None,
        "type_extracted": state.invoice_type_id,
        "type_truth": truth.get("invoice_type_id"),
        "field_results": {},
        "compliance_results": {},
        "score": {},
        "human_notes": truth.get("notes", ""),
    }

    # --- Type ---
    if results["type_truth"]:
        results["type_match"] = (state.invoice_type_id == results["type_truth"])

    # --- Fields ---
    truth_fields = truth.get("fields", {})
    all_field_names = set(truth_fields.keys()) | set(state.extracted_fields.keys())
    field_correct = 0
    field_total = 0

    for fname in all_field_names:
        truth_val = truth_fields.get(fname)
        extracted_result = state.extracted_fields.get(fname)
        extracted_val = extracted_result.extracted_value if extracted_result else None

        cmp = _compare_value(extracted_val, truth_val, fname)
        results["field_results"][fname] = {
            **cmp,
            "confidence": extracted_result.confidence if extracted_result else 0.0,
            "flagged": extracted_result.flagged_for_review if extracted_result else False,
        }
        if cmp["match"] is not None:
            field_total += 1
            if cmp["match"] or cmp["partial"]:
                field_correct += 1

    # --- Compliance ---
    truth_compliance = truth.get("compliance", {})
    rule_correct = 0
    rule_total = 0

    agent_compliance = {r.rule_id: r.status for r in state.rule_results}
    all_rules = set(truth_compliance.keys()) | set(agent_compliance.keys())

    for rule_id in all_rules:
        truth_status = truth_compliance.get(rule_id)
        agent_status = agent_compliance.get(rule_id)
        if truth_status:
            match = (agent_status == truth_status)
            results["compliance_results"][rule_id] = {
                "match": match,
                "extracted": agent_status,
                "truth": truth_status,
            }
            rule_total += 1
            if match:
                rule_correct += 1

    # --- Score ---
    results["score"] = {
        "type_correct": results["type_match"],
        "fields_correct": field_correct,
        "fields_total": field_total,
        "field_accuracy": round(field_correct / field_total, 2) if field_total else None,
        "rules_correct": rule_correct,
        "rules_total": rule_total,
        "rule_accuracy": round(rule_correct / rule_total, 2) if rule_total else None,
    }

    return results


def format_diff_for_agent(diff: dict) -> str:
    """
    Render the diff as a readable text block to pass to the reflection prompt.
    """
    lines = ["=== GROUND TRUTH COMPARISON ===\n"]

    # Type
    type_match = diff["type_match"]
    if type_match is True:
        lines.append(f"DOCUMENT TYPE: ✓ Correct ({diff['type_extracted']})")
    elif type_match is False:
        lines.append(f"DOCUMENT TYPE: ✗ Wrong — you detected '{diff['type_extracted']}', correct is '{diff['type_truth']}'")
    else:
        lines.append(f"DOCUMENT TYPE: (no ground truth provided) — you detected '{diff['type_extracted']}'")

    # Fields
    lines.append("\nFIELD RESULTS:")
    for fname, r in diff["field_results"].items():
        if r["match"] is None:
            continue  # no ground truth
        if r["match"]:
            status = "✓"
        elif r["partial"]:
            status = "~ (partial)"
        else:
            status = "✗"
        ext = r["extracted"] if r["extracted"] is not None else "(not found)"
        tru = r["truth"]
        note = f" [{r['note']}]" if r.get("note") else ""
        conf = f" confidence={r['confidence']:.2f}" if r["confidence"] else ""
        lines.append(f"  {status} {fname}: extracted='{ext}'{conf} | truth='{tru}'{note}")

    # Compliance
    if diff["compliance_results"]:
        lines.append("\nCOMPLIANCE RESULTS:")
        for rule_id, r in diff["compliance_results"].items():
            status = "✓" if r["match"] else "✗"
            lines.append(f"  {status} {rule_id}: agent='{r['extracted']}' | truth='{r['truth']}'")

    # Score
    s = diff["score"]
    lines.append(f"\nSCORE SUMMARY:")
    if s["field_accuracy"] is not None:
        lines.append(f"  Fields:     {s['fields_correct']}/{s['fields_total']} correct ({s['field_accuracy']*100:.0f}%)")
    if s["rule_accuracy"] is not None:
        lines.append(f"  Compliance: {s['rules_correct']}/{s['rules_total']} correct ({s['rule_accuracy']*100:.0f}%)")

    if diff.get("human_notes"):
        lines.append(f"\nHUMAN NOTES: {diff['human_notes']}")

    return "\n".join(lines)
