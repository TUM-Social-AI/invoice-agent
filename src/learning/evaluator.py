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

from __future__ import annotations

import csv
import difflib
import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from src.agent.state import AgentState

if TYPE_CHECKING:
    from src.config.loader import ConfigStore

logger = logging.getLogger(__name__)

NUMERIC_TOLERANCE = 0.02      # for amounts
RATE_TOLERANCE = 0.001     # for percentages/rates
STRING_SIMILARITY_THRESHOLD = 0.7  # SequenceMatcher ratio; 1.0 = identical
STRING_SIMILARITY_MIN_LEN = 4      # both strings must be at least this long for similarity


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


def _ground_truth_match_key(label: str) -> str:
    """
    Normalise a PDF filename or CSV Source cell for fuzzy matching.

    Pathlib's `.stem` must not be used on strings like ``A.5.d.- foo`` that lack ``.pdf``:
    it treats the first ``.`` as the extension, producing a useless short stem (e.g. ``A.5.d``).
    """
    s = str(label).strip()
    if s.lower().endswith(".pdf"):
        base = Path(s).stem
    else:
        base = s
    return re.sub(r"[^a-z0-9]+", "", base.lower())


def _resolve_ground_truth_csv_path(cfg: dict | None, invoice_type_id: str | None) -> Optional[str]:
    if not cfg:
        return None
    by_type = cfg.get("ground_truth_csv_by_invoice_type")
    if isinstance(by_type, dict) and invoice_type_id and str(invoice_type_id).strip():
        p = by_type.get(str(invoice_type_id).strip())
        if p:
            return str(p)
    p = cfg.get("ground_truth_csv_path")
    return str(p) if p else None


def _truth_dict_from_csv_row(matched_row: dict, cfg: dict) -> dict:
    col_map: dict = cfg.get("ground_truth_column_map") or {}
    invoice_type_col = cfg.get("ground_truth_invoice_type_column")

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
        fields[str(field_name).strip()] = raw_s

    truth: dict = {"fields": fields}
    if invoice_type_col and invoice_type_col in matched_row:
        truth["invoice_type_id"] = (matched_row.get(invoice_type_col) or "").strip() or None
    return truth


def load_ground_truth(
    pdf_path: str,
    config: dict | None = None,
    invoice_type_id: str | None = None,
) -> Optional[dict]:
    """
    Find and load the ground truth.

    1) Prefer `<pdf_stem>_truth.json` next to the PDF (existing behavior).
    2) If missing, resolve CSV path from `ground_truth_csv_by_invoice_type[invoice_type_id]`
       or `ground_truth_csv_path`, then load a matching row.
    """
    truth_path = Path(pdf_path).with_name(Path(pdf_path).stem + "_truth.json")
    if truth_path.exists():
        try:
            return json.loads(truth_path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"Could not load ground truth {truth_path}: {e}")
            return None

    cfg = (config or {}).get("agent", {}) if config else {}
    csv_path = _resolve_ground_truth_csv_path(cfg, invoice_type_id)
    if not csv_path:
        return None

    csv_file = Path(csv_path)
    if not csv_file.exists():
        logger.warning(f"Ground truth CSV configured but not found: {csv_file}")
        return None

    source_col = str(cfg.get("ground_truth_source_column", "Source file"))

    pdf_key = _ground_truth_match_key(Path(pdf_path).name)

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
                sk = _ground_truth_match_key(v)
                if sk == pdf_key or pdf_key in sk or sk in pdf_key:
                    matched_row = row
                    break
    except Exception as e:
        logger.warning(f"Could not read ground truth CSV {csv_file}: {e}")
        return None

    if not matched_row:
        logger.info(
            "Ground truth: no CSV row matched PDF '%s' (source column '%s' in %s).",
            Path(pdf_path).name,
            source_col,
            csv_file,
        )
        return None

    return _truth_dict_from_csv_row(matched_row, cfg)


def ground_truth_csv_configured(config: dict | None) -> bool:
    """True if any CSV-based ground truth path is set in config."""
    cfg = (config or {}).get("agent", {}) if config else {}
    if cfg.get("ground_truth_csv_path"):
        return True
    bt = cfg.get("ground_truth_csv_by_invoice_type")
    return isinstance(bt, dict) and bool(bt)


# ---------------------------------------------------------------------------
# Value comparison helpers
# ---------------------------------------------------------------------------

def _normalize_number(v) -> Optional[float]:
    try:
        n = _normalize_number_for_eval(v)
        return float(n) if n is not None else None
    except Exception:
        return None


def _normalize_date_value(
    v: Any,
    slash_order: str = "DMY",
    *,
    log: Optional[logging.Logger] = None,
    field_name: str = "",
) -> Optional[str]:
    """
    Normalise a date string to YYYY-MM-DD.

    - ISO YYYY-MM-DD / YYYY/MM/DD (single-digit month/day allowed in regex below).
    - Slash/dot/dash numeric: unambiguous when first token > 12 (DMY day-first) or
      second token > 12 (MDY month-first). When both tokens are ≤ 12, use `slash_order`.
    """
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None

    # YYYY-MM-DD or YYYY/MM/DD / YYYY.MM.DD
    m = re.match(r"^(\d{4})[/.\-](\d{1,2})[/.\-](\d{1,2})$", s)
    if m:
        return f"{m.group(1)}-{m.group(2).zfill(2)}-{m.group(3).zfill(2)}"

    # DD/MM/YYYY style (two 1–2 digit groups + 4 digit year)
    m = re.match(r"^(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})$", s)
    if not m:
        return s.lower()

    a, b = int(m.group(1)), int(m.group(2))
    y = int(m.group(3))
    order = (slash_order or "DMY").strip().upper()
    if order not in ("DMY", "MDY", "YMD"):
        order = "DMY"

    if a > 12:
        day, month = a, b
    elif b > 12:
        month, day = a, b
    else:
        if order == "MDY":
            month, day = a, b
        else:
            day, month = a, b
        if log is not None:
            log.debug(
                "Ambiguous slash date for field %r: %r — interpreted as %s (month=%02d day=%02d). "
                "Prefer ISO YYYY-MM-DD in ground truth CSV.",
                field_name or "?",
                s,
                order,
                month,
                day,
            )

    if not (1 <= month <= 12 and 1 <= day <= 31):
        return s.lower()

    return f"{y}-{month:02d}-{day:02d}"


def _normalize_string(v) -> str:
    if v is None:
        return ""
    return str(v).strip().lower()


def _string_similarity_partial(
    ext_s: str,
    tru_s: str,
    threshold: float = STRING_SIMILARITY_THRESHOLD,
    min_len: int = STRING_SIMILARITY_MIN_LEN,
) -> bool:
    if not ext_s or not tru_s:
        return False
    # Fallback for very short tokens (currency codes, language tags, etc.)
    if len(ext_s) < min_len or len(tru_s) < min_len:
        return tru_s in ext_s or ext_s in tru_s
    # Fast-path substring check
    if tru_s in ext_s or ext_s in tru_s:
        return True
    ratio = difflib.SequenceMatcher(None, ext_s, tru_s).ratio()
    return ratio >= threshold


def _compare_pay_period(extracted, truth_val, *, date_parse: str, log: Optional[logging.Logger]) -> dict:
    """
    Pay periods are not scalars: bare month (8) must not be compared to year (2025) as numbers.
    Prefer year overlap when truth is year-only; otherwise string/date normalization.
    """
    ext_s_raw = str(extracted).strip()
    tru_s_raw = str(truth_val).strip()
    ext_num = _normalize_number(extracted)
    tru_num = _normalize_number(truth_val)

    if ext_num is not None and tru_num is not None:
        # Month-only (1–12) vs year (e.g. 2025) — invalid numeric comparison
        if 1 <= ext_num <= 12 and 1900 <= tru_num <= 2100:
            return {
                "match": False,
                "partial": False,
                "extracted": ext_num,
                "truth": tru_num,
                "note": "extracted looks like month number only; truth is a year — use MM/YYYY or text (e.g. Août 2025) in extraction and CSV",
            }
        if 1 <= tru_num <= 12 and 1900 <= ext_num <= 2100:
            return {
                "match": False,
                "partial": False,
                "extracted": ext_num,
                "truth": tru_num,
                "note": "truth looks like month-only; extracted is year-like — align period formats",
            }
        # Two year-like values (e.g. 2025 vs 2025)
        if 1900 <= ext_num <= 2100 and 1900 <= tru_num <= 2100:
            ok = abs(ext_num - tru_num) <= NUMERIC_TOLERANCE
            return {
                "match": ok,
                "partial": False,
                "extracted": ext_num,
                "truth": tru_num,
                "note": "" if ok else f"year diff: {abs(ext_num - tru_num):.4f}",
            }

    ext_d = _normalize_date_value(extracted, slash_order=date_parse, log=log, field_name="pay_period")
    tru_d = _normalize_date_value(truth_val, slash_order=date_parse, log=log, field_name="pay_period")
    if ext_d == tru_d:
        return {"match": True, "partial": False, "extracted": ext_d, "truth": tru_d, "note": ""}

    years_e = set(re.findall(r"\b(19\d{2}|20\d{2})\b", ext_s_raw))
    years_t = set(re.findall(r"\b(19\d{2}|20\d{2})\b", tru_s_raw))
    if years_e and years_t and years_e & years_t:
        return {
            "match": True,
            "partial": False,
            "extracted": ext_s_raw,
            "truth": tru_s_raw,
            "note": "same calendar year in both values",
        }

    ext_s = _normalize_string(extracted)
    tru_s = _normalize_string(truth_val)
    exact = ext_s == tru_s
    partial = (not exact) and _string_similarity_partial(ext_s, tru_s)
    return {
        "match": exact,
        "partial": partial,
        "extracted": str(extracted),
        "truth": str(truth_val),
        "note": "partial match" if partial else ("" if exact else "mismatch"),
    }


def _looks_like_short_date_fragment(s: str) -> bool:
    t = str(s).strip()
    return bool(re.match(r"^\d{1,2}[/.\-]\d{1,2}([/.\-]\d{2,4})?$", t))


def _compare_value(
    extracted,
    truth_val,
    field_name: str,
    *,
    date_parse: str = "DMY",
    log: Optional[logging.Logger] = None,
) -> dict:
    """
    Compare one extracted value against the ground truth.
    Returns {"match": bool|None, "partial": bool, "extracted": ..., "truth": ...}
    """
    if truth_val is None:
        return {"match": None, "partial": False, "extracted": extracted, "truth": truth_val,
                "note": "no ground truth provided for this field"}

    # Null extracted
    if extracted is None or str(extracted).lower() in ("null", "none", ""):
        return {"match": False, "partial": False, "extracted": None, "truth": truth_val,
                "note": "field not extracted"}

    if field_name == "pay_period":
        return _compare_pay_period(extracted, truth_val, date_parse=date_parse, log=log)

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
        ext_d = _normalize_date_value(extracted, slash_order=date_parse, log=log, field_name=field_name)
        tru_d = _normalize_date_value(truth_val, slash_order=date_parse, log=log, field_name=field_name)
        ok = ext_d == tru_d
        return {"match": ok, "partial": False, "extracted": ext_d, "truth": tru_d,
                "note": "" if ok else f"date mismatch: '{ext_d}' vs '{tru_d}'"}

    # String comparison
    if field_name == "payment_method" and _looks_like_short_date_fragment(str(extracted)):
        return {
            "match": False,
            "partial": False,
            "extracted": str(extracted),
            "truth": str(truth_val),
            "note": "extracted looks like a date fragment — use payer line or bank/cash wording",
        }

    ext_s = _normalize_string(extracted)
    tru_s = _normalize_string(truth_val)
    exact = ext_s == tru_s
    partial = (not exact) and _string_similarity_partial(ext_s, tru_s)
    note = "partial match" if partial else ("" if exact else "mismatch")
    if (
        field_name == "expense_category"
        and not exact
        and re.match(r"^\d{1,2}\.\d{2}$", str(extracted).strip())
    ):
        note = f"{note} — prefer printed category label over budget code"

    return {
        "match": exact,
        "partial": partial,
        "extracted": str(extracted),
        "truth": str(truth_val),
        "note": note,
    }


# ---------------------------------------------------------------------------
# Main evaluation function
# ---------------------------------------------------------------------------

def evaluate(
    state: AgentState,
    truth: dict,
    store: Optional[ConfigStore] = None,
    date_parse: str = "DMY",
) -> dict:
    """
    Compare agent state against ground truth.
    When `store` is set, only field_names present on the classified invoice type's schema
    are compared (wide CSV rows can include columns for other types).
    """
    results: dict[str, Any] = {
        "type_match": None,
        "type_extracted": state.invoice_type_id,
        "type_truth": truth.get("invoice_type_id"),
        "field_results": {},
        "compliance_results": {},
        "score": {},
        "human_notes": truth.get("notes", ""),
    }

    allowed: Optional[set[str]] = None
    if store is not None and state.invoice_type_id:
        allowed = {f.field_name for f in store.get_fields(state.invoice_type_id)}

    truth_fields_raw = truth.get("fields", {})
    if allowed is not None:
        truth_fields = {k: v for k, v in truth_fields_raw.items() if k in allowed}
    else:
        truth_fields = dict(truth_fields_raw)

    extracted_keys = set(state.extracted_fields.keys())
    if allowed is not None:
        extracted_keys = extracted_keys & allowed

    # --- Type ---
    if results["type_truth"]:
        results["type_match"] = (state.invoice_type_id == results["type_truth"])

    # --- Fields ---
    all_field_names = set(truth_fields.keys()) | extracted_keys
    fields_exact = 0
    fields_partial = 0
    fields_wrong = 0
    fields_not_extracted = 0
    fields_wrong_value = 0
    field_total = 0

    for fname in sorted(all_field_names):
        truth_val = truth_fields.get(fname)
        extracted_result = state.extracted_fields.get(fname)
        extracted_val = extracted_result.extracted_value if extracted_result else None

        cmp = _compare_value(
            extracted_val, truth_val, fname, date_parse=date_parse, log=logger,
        )
        results["field_results"][fname] = {
            **cmp,
            "confidence": extracted_result.confidence if extracted_result else 0.0,
            "flagged": extracted_result.flagged_for_review if extracted_result else False,
        }
        if cmp["match"] is None:
            continue
        field_total += 1
        if cmp["match"] is True:
            fields_exact += 1
        elif cmp.get("partial"):
            fields_partial += 1
        else:
            fields_wrong += 1
            if cmp.get("note") == "field not extracted" or cmp.get("extracted") is None:
                fields_not_extracted += 1
            else:
                fields_wrong_value += 1

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

    matched_for_accuracy = fields_exact + fields_partial
    results["score"] = {
        "type_correct": results["type_match"],
        "fields_exact": fields_exact,
        "fields_partial": fields_partial,
        "fields_wrong": fields_wrong,
        "fields_not_extracted": fields_not_extracted,
        "fields_wrong_value": fields_wrong_value,
        "fields_total": field_total,
        "fields_correct": matched_for_accuracy,
        # "field_accuracy" = lenient match rate (exact + partial); "exact_accuracy" = strict.
        "field_accuracy": round(matched_for_accuracy / field_total, 4) if field_total else None,
        "exact_accuracy": round(fields_exact / field_total, 4) if field_total else None,
        "rules_correct": rule_correct,
        "rules_total": rule_total,
        "rule_accuracy": round(rule_correct / rule_total, 4) if rule_total else None,
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
    lines.append("\nSCORE SUMMARY (fields that have ground truth only):")
    if s.get("field_accuracy") is not None:
        ft = s["fields_total"]
        lines.append(
            f"  Exact:   {s.get('fields_exact', 0)}/{ft}  |  "
            f"Partial: {s.get('fields_partial', 0)}/{ft}  |  "
            f"Wrong:   {s.get('fields_wrong', 0)}/{ft}  "
            f"(missing: {s.get('fields_not_extracted', 0)}, value mismatch: {s.get('fields_wrong_value', 0)})"
        )
        lines.append(
            f"  Lenient match rate (exact + partial): {s['field_accuracy']*100:.1f}%  |  "
            f"Strict (exact only): {s.get('exact_accuracy', 0)*100:.1f}%"
        )
    if s.get("rule_accuracy") is not None:
        lines.append(
            f"  Compliance vs truth: {s['rules_correct']}/{s['rules_total']} rules "
            f"({s['rule_accuracy']*100:.1f}%)"
        )

    if diff.get("human_notes"):
        lines.append(f"\nHUMAN NOTES: {diff['human_notes']}")

    return "\n".join(lines)
