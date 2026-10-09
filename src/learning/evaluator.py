"""
Learning mode evaluator.

Loads a ground truth JSON file and compares it against the agent's results.
Returns a structured diff that is passed to the reflection loop in agent.py.

Ground truth file: <pdf_stem>_truth.json alongside the PDF, or a matching row in
`agent.ground_truth_csv_path`. Reviewer compliance findings come from
`agent.compliance_ground_truth_csv_path` (one row per PDF and issue_code) and are
attached as `truth["compliance_review"]`.

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

from src.agent.state import AgentState, rule_verdict_summary
from src.sources.models import SourceProvenance
from src.tools.value_parsing import parse_amount

if TYPE_CHECKING:
    from src.config.loader import ConfigStore

logger = logging.getLogger(__name__)

NUMERIC_TOLERANCE = 0.02      # for amounts
RATE_TOLERANCE = 0.001     # for percentages/rates
STRING_SIMILARITY_THRESHOLD = 0.7  # SequenceMatcher ratio; 1.0 = identical
STRING_SIMILARITY_MIN_LEN = 4      # both strings must be at least this long for similarity
# Reviewer issue codes that need documents the agent never sees; used when config sets none.
DEFAULT_UNSCORED_ISSUE_CODES = ("external_documents",)
SCORED_ISSUE_LEVELS = ("fail", "verify")


def _normalize_number_for_eval(v) -> Optional[float]:
    """A number only when the whole value is an amount: "0007932" is, "006/09/2025" is not."""
    return parse_amount(v, strict=True)


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
    candidate_groups: list[list[str]] = []
    for csv_col, target in col_map.items():
        if csv_col not in matched_row:
            continue
        raw = matched_row.get(csv_col)
        if raw is None:
            continue
        raw_s = str(raw).strip()
        if not raw_s:
            continue
        # A column may list candidate field names in priority order (e.g. Client Name ->
        # [beneficiary, client_name]); evaluate() keeps the first one the invoice type has.
        names = [str(n).strip() for n in target] if isinstance(target, (list, tuple)) else [str(target).strip()]
        names = [n for n in names if n]
        for name in names:
            fields[name] = raw_s
        if len(names) > 1:
            candidate_groups.append(names)

    truth: dict = {"fields": fields}
    if candidate_groups:
        truth["field_candidates"] = candidate_groups
    if invoice_type_col and invoice_type_col in matched_row:
        truth["invoice_type_id"] = (matched_row.get(invoice_type_col) or "").strip() or None
    return truth


def _multi_invoice_truth(rows: list[dict], cfg: dict) -> dict:
    """
    Several ground-truth rows share one PDF (e.g. two invoices scanned into one file).
    The pipeline extracts one invoice per PDF, so scoring fields against any single row
    would report a partial document as if it were complete. Keep the PDF-level truth
    (invoice type when all rows agree) and skip field scoring.
    """
    id_col = str(cfg.get("ground_truth_id_column", "Id"))
    row_ids = [(r.get(id_col) or "").strip() or "?" for r in rows]
    truth: dict = {
        "fields": {},
        "multi_invoice": {
            "row_ids": row_ids,
            "reason": (
                f"{len(rows)} ground-truth rows ({', '.join(row_ids)}) share this source file; "
                "the pipeline extracts one invoice per PDF, so field scoring is skipped"
            ),
        },
    }
    types = {_truth_dict_from_csv_row(r, cfg).get("invoice_type_id") for r in rows} - {None}
    if len(types) == 1:
        truth["invoice_type_id"] = types.pop()
    return truth


def _read_matching_csv_rows(
    csv_file: Path,
    pdf_path: str,
    cfg: dict,
    source_provenance: SourceProvenance | None,
) -> Optional[list[dict]]:
    """
    Return every CSV row that refers to the same source as the first match for `pdf_path`
    (source id/revision when available, else the normalised Source file key).
    Returns None when the CSV cannot be used, [] when nothing matches.
    """
    source_col = str(cfg.get("ground_truth_source_column", "Source file"))
    source_id_col = cfg.get("ground_truth_source_id_column")
    revision_col = cfg.get("ground_truth_revision_column")

    pdf_key = _ground_truth_match_key(Path(pdf_path).name)

    try:
        with open(csv_file, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            fieldnames = reader.fieldnames or []
            rows = list(reader)
    except Exception as e:
        logger.warning(f"Could not read ground truth CSV {csv_file}: {e}")
        return None

    source_id_col_s = str(source_id_col) if source_id_col else ""
    revision_col_s = str(revision_col) if revision_col else ""
    has_source_file_col = source_col in fieldnames
    has_source_id_col = bool(source_id_col_s and source_id_col_s in fieldnames)
    if not fieldnames or (not has_source_file_col and not has_source_id_col):
        logger.warning(
            f"Ground truth CSV missing source column '{source_col}'"
            f"{f' or source id column {source_id_col_s!r}' if source_id_col_s else ''}. "
            f"Found columns: {fieldnames}"
        )
        return None

    def _row_id_key(row: dict) -> tuple[str, str]:
        return (
            (row.get(source_id_col_s) or "").strip(),
            (row.get(revision_col_s) or "").strip() if revision_col_s else "",
        )

    for row in rows:
        if source_provenance and has_source_id_col:
            row_source_id, row_revision = _row_id_key(row)
            revision_matches = (
                not revision_col_s
                or not row_revision
                or row_revision == (source_provenance.revision_id or "")
            )
            if row_source_id and row_source_id == source_provenance.source_id and revision_matches:
                key = _row_id_key(row)
                return [r for r in rows if _row_id_key(r) == key]
            if row_source_id:
                continue
        if not has_source_file_col:
            continue
        v = row.get(source_col, "")
        if not v:
            continue
        sk = _ground_truth_match_key(v)
        if sk == pdf_key or pdf_key in sk or sk in pdf_key:
            return [
                r for r in rows
                if r.get(source_col) and _ground_truth_match_key(r[source_col]) == sk
            ]
    return []


def _parse_compliant_flag(raw: Any) -> Optional[bool]:
    s = str(raw or "").strip().lower()
    if s in ("yes", "y", "si", "sí", "true", "1"):
        return True
    if s in ("no", "n", "false", "0"):
        return False
    return None


def load_compliance_review(
    pdf_path: str,
    config: dict | None = None,
    source_provenance: SourceProvenance | None = None,
) -> Optional[dict]:
    """
    Load the reviewer's compliance findings for one PDF from
    `agent.compliance_ground_truth_csv_path` (columns: Id, Source file, overall_compliant,
    issue_code, level, note). Returns {"overall_compliant": bool|None, "issues": [...]}
    or None when no row matches. A blank overall_compliant means the reviewer gave no verdict.
    """
    cfg = (config or {}).get("agent", {}) if config else {}
    path = cfg.get("compliance_ground_truth_csv_path")
    if not path:
        return None
    csv_file = Path(str(path))
    if not csv_file.exists():
        logger.warning(f"Compliance ground truth CSV configured but not found: {csv_file}")
        return None

    rows = _read_matching_csv_rows(csv_file, pdf_path, cfg, source_provenance)
    if not rows:
        return None

    verdicts = {_parse_compliant_flag(r.get("overall_compliant")) for r in rows} - {None}
    if len(verdicts) > 1:
        logger.warning(
            "Compliance ground truth: conflicting overall_compliant values for '%s'; treating as unknown.",
            Path(pdf_path).name,
        )
    overall = verdicts.pop() if len(verdicts) == 1 else None

    id_col = str(cfg.get("ground_truth_id_column", "Id"))
    issues = []
    for r in rows:
        code = (r.get("issue_code") or "").strip()
        if not code:
            continue
        issues.append({
            "id": (r.get(id_col) or "").strip(),
            "issue_code": code,
            "level": (r.get("level") or "").strip().lower(),
            "note": (r.get("note") or "").strip(),
        })
    return {"overall_compliant": overall, "issues": issues}


def load_ground_truth(
    pdf_path: str,
    config: dict | None = None,
    invoice_type_id: str | None = None,
    source_provenance: SourceProvenance | None = None,
) -> Optional[dict]:
    """
    Find and load the ground truth.

    1) Prefer `<pdf_stem>_truth.json` next to the PDF (existing behavior).
    2) If missing, resolve CSV path from `ground_truth_csv_by_invoice_type[invoice_type_id]`
       or `ground_truth_csv_path`, then load a matching row. Several rows for one PDF yield
       a `multi_invoice` truth without fields.
    3) Attach reviewer findings from the compliance CSV as `compliance_review`; a PDF with
       only compliance truth gets `{"fields": {}, "compliance_review": ...}`.
    """
    review = load_compliance_review(pdf_path, config=config, source_provenance=source_provenance)
    truth = _load_field_truth(pdf_path, config, invoice_type_id, source_provenance)
    if review is not None and (truth is None or "compliance_review" not in truth):
        truth = truth if truth is not None else {"fields": {}}
        truth["compliance_review"] = review
    return truth


def _load_field_truth(
    pdf_path: str,
    config: dict | None,
    invoice_type_id: str | None,
    source_provenance: SourceProvenance | None,
) -> Optional[dict]:
    local_sibling_truth_allowed = (
        source_provenance is None
        or source_provenance.source_type == "local"
    )
    truth_path = Path(pdf_path).with_name(Path(pdf_path).stem + "_truth.json")
    if local_sibling_truth_allowed and truth_path.exists():
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

    rows = _read_matching_csv_rows(csv_file, pdf_path, cfg, source_provenance)
    if not rows:
        if rows is not None:
            logger.info(
                "Ground truth: no CSV row matched PDF '%s' (source column '%s' in %s).",
                Path(pdf_path).name,
                cfg.get("ground_truth_source_column", "Source file"),
                csv_file,
            )
        return None

    if len(rows) > 1:
        truth = _multi_invoice_truth(rows, cfg)
        logger.info("Ground truth for '%s': %s", Path(pdf_path).name, truth["multi_invoice"]["reason"])
        return truth
    return _truth_dict_from_csv_row(rows[0], cfg)


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

    # Date fields must not enter numeric comparison — ISO dates like "2025-11-09" are
    # spuriously parsed as the year 2025 by _normalize_number, producing false matches.
    is_date_field = any(kw in field_name for kw in ("date", "fecha"))

    if not is_date_field:
        ext_num = _normalize_number(extracted)
        tru_num = _normalize_number(truth_val)
        if ext_num is not None and tru_num is not None:
            tol = RATE_TOLERANCE if "rate" in field_name or "pct" in field_name else NUMERIC_TOLERANCE
            ok = abs(ext_num - tru_num) <= tol
            return {"match": ok, "partial": False, "extracted": ext_num, "truth": tru_num,
                    "note": f"numeric diff: {abs(ext_num - tru_num):.4f}"}

    # Try date comparison
    if is_date_field or any(kw in field_name for kw in ("period", "periodo")):
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


def _alias_key(v: Any) -> str:
    return re.sub(r"\s+", " ", str(v)).strip().casefold()


def _build_value_aliases(raw: Any) -> dict[str, dict[str, str]]:
    """
    Config shape: {field_name: {canonical: [alias, ...]}}. Returns
    {field_name: {normalised alias or canonical: canonical}}.
    """
    out: dict[str, dict[str, str]] = {}
    if not isinstance(raw, dict):
        return out
    for field_name, groups in raw.items():
        if not isinstance(groups, dict):
            continue
        lookup: dict[str, str] = {}
        for canonical, aliases in groups.items():
            lookup[_alias_key(canonical)] = str(canonical)
            for alias in aliases if isinstance(aliases, (list, tuple)) else [aliases]:
                lookup[_alias_key(alias)] = str(canonical)
        out[str(field_name)] = lookup
    return out


def _apply_value_alias(value: Any, field_name: str, aliases: dict[str, dict[str, str]]) -> Any:
    lookup = aliases.get(field_name)
    if not lookup or value is None:
        return value
    return lookup.get(_alias_key(value), value)


def _issue_codes_by_rule(store: Optional[ConfigStore]) -> dict[str, str]:
    if store is None:
        return {}
    out: dict[str, str] = {}
    for rules in store.compliance_rules.values():
        for rule in rules:
            code = (getattr(rule, "issue_code", "") or "").strip()
            if code:
                out[rule.rule_id] = code
    return out


def _score_compliance_review(
    state: AgentState,
    review: dict,
    store: Optional[ConfigStore],
    unscored_codes: set[str],
) -> dict:
    """
    Match reviewer issues against the agent's rule results via each rule's issue_code.
    An issue is caught when at least one rule with that issue_code failed (any severity).
    Skipped and flagged rules never count as caught; they are listed separately.
    """
    code_by_rule = _issue_codes_by_rule(store)
    by_status: dict[str, dict[str, list[str]]] = {"failed": {}, "skipped": {}, "flagged": {}}
    failed_without_code: list[str] = []
    for r in state.rule_results:
        code = code_by_rule.get(r.rule_id)
        if r.status == "failed" and not code:
            failed_without_code.append(r.rule_id)
        if code and r.status in by_status:
            by_status[r.status].setdefault(code, []).append(r.rule_id)
    raised = by_status["failed"]

    issues_out: list[dict] = []
    unscored: list[dict] = []
    counts = {level: {"caught": 0, "total": 0} for level in SCORED_ISSUE_LEVELS}
    for issue in review.get("issues", []):
        code = issue["issue_code"]
        level = issue.get("level", "")
        if code in unscored_codes or level not in counts:
            unscored.append(issue)
            continue
        rule_ids = raised.get(code, [])
        issues_out.append({
            **issue,
            "caught": bool(rule_ids),
            "rule_ids": rule_ids,
            "skipped_rule_ids": by_status["skipped"].get(code, []),
            "flagged_rule_ids": by_status["flagged"].get(code, []),
        })
        counts[level]["total"] += 1
        if rule_ids:
            counts[level]["caught"] += 1

    reviewer_codes = {i["issue_code"] for i in review.get("issues", [])}
    # Codes the agent raised that the reviewer did not mention. Not necessarily wrong:
    # the reviewer may have missed them.
    false_alarms = {c: ids for c, ids in raised.items() if c not in reviewer_codes}

    truth_overall = review.get("overall_compliant")
    agent_compliant = not rule_verdict_summary(state.rule_results)["error_failed_rule_ids"]
    return {
        "issues": issues_out,
        "unscored_issues": unscored,
        "false_alarms": false_alarms,
        "failed_rules_without_issue_code": failed_without_code,
        "agent_compliant": agent_compliant,
        "truth_compliant": truth_overall,
        "overall_match": None if truth_overall is None else (agent_compliant == truth_overall),
        "counts": counts,
    }


# ---------------------------------------------------------------------------
# Main evaluation function
# ---------------------------------------------------------------------------

def evaluate(
    state: AgentState,
    truth: dict,
    store: Optional[ConfigStore] = None,
    date_parse: str = "DMY",
    config: dict | None = None,
) -> dict:
    """
    Compare agent state against ground truth.
    When `store` is set, only field_names present on the classified invoice type's schema
    are compared (wide CSV rows can include columns for other types).
    `config` (app config) supplies `agent.ground_truth_value_aliases` and
    `agent.compliance_unscored_issue_codes`.
    """
    agent_cfg = (config or {}).get("agent", {}) if config else {}
    value_aliases = _build_value_aliases(agent_cfg.get("ground_truth_value_aliases"))
    unscored_cfg = agent_cfg.get("compliance_unscored_issue_codes")
    unscored_codes = set(unscored_cfg if unscored_cfg is not None else DEFAULT_UNSCORED_ISSUE_CODES)

    results: dict[str, Any] = {
        "type_match": None,
        "type_extracted": state.invoice_type_id,
        "type_truth": truth.get("invoice_type_id"),
        "field_results": {},
        "compliance_results": {},
        "compliance_review": None,
        "field_scoring_skipped": None,
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

    for group in truth.get("field_candidates") or []:
        present = [name for name in group if name in truth_fields]
        for name in present[1:]:
            truth_fields.pop(name, None)

    extracted_keys = set(state.extracted_fields.keys())
    if allowed is not None:
        extracted_keys = extracted_keys & allowed

    multi = truth.get("multi_invoice")
    if multi:
        results["field_scoring_skipped"] = multi.get("reason") or "multi-invoice PDF"
        truth_fields = {}
        extracted_keys = set()

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
            _apply_value_alias(extracted_val, fname, value_aliases),
            _apply_value_alias(truth_val, fname, value_aliases),
            fname,
            date_parse=date_parse,
            log=logger,
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

    # --- Compliance vs reviewer issues (compliance ground truth CSV) ---
    review = truth.get("compliance_review")
    review_score: dict[str, Any] = {}
    if review is not None:
        rv = _score_compliance_review(state, review, store, unscored_codes)
        results["compliance_review"] = rv
        for level in SCORED_ISSUE_LEVELS:
            c = rv["counts"][level]
            review_score[f"issues_{level}_caught"] = c["caught"]
            review_score[f"issues_{level}_total"] = c["total"]
            review_score[f"issue_{level}_recall"] = (
                round(c["caught"] / c["total"], 4) if c["total"] else None
            )
        review_score["issue_false_alarms"] = len(rv["false_alarms"])
        review_score["overall_compliant_match"] = rv["overall_match"]

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
        **review_score,
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
    if diff.get("field_scoring_skipped"):
        lines.append(f"  (skipped: {diff['field_scoring_skipped']})")
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

    rv = diff.get("compliance_review")
    if rv:
        lines.append("\nREVIEWER COMPLIANCE ISSUES:")
        for issue in rv["issues"]:
            status = "✓ caught" if issue["caught"] else "✗ missed"
            via = f" by {', '.join(issue['rule_ids'])}" if issue["rule_ids"] else ""
            skipped = (
                f" (skipped: {', '.join(issue['skipped_rule_ids'])})" if issue["skipped_rule_ids"] else ""
            )
            lines.append(
                f"  {status} [{issue['level']}] {issue['issue_code']}{via}{skipped}: {issue['note']}"
            )
        for issue in rv["unscored_issues"]:
            lines.append(f"  - not scored: {issue['issue_code']}: {issue['note']}")
        for code, rule_ids in rv["false_alarms"].items():
            lines.append(f"  ? raised but not in review: {code} ({', '.join(rule_ids)})")
        if rv["truth_compliant"] is not None:
            mark = "✓" if rv["overall_match"] else "✗"
            lines.append(
                f"  {mark} overall: agent={'compliant' if rv['agent_compliant'] else 'non-compliant'} | "
                f"reviewer={'compliant' if rv['truth_compliant'] else 'non-compliant'}"
            )

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

    for level in SCORED_ISSUE_LEVELS:
        if s.get(f"issue_{level}_recall") is not None:
            lines.append(
                f"  Reviewer '{level}' issues caught: {s[f'issues_{level}_caught']}/{s[f'issues_{level}_total']} "
                f"({s[f'issue_{level}_recall']*100:.1f}%)"
            )

    if diff.get("human_notes"):
        lines.append(f"\nHUMAN NOTES: {diff['human_notes']}")

    return "\n".join(lines)
