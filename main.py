#!/usr/bin/env python3
"""
Invoice Compliance Agent — CLI entrypoint.

Usage:
  python main.py                                         # process all PDFs in invoices/
  python main.py --pdf invoices/invoice.pdf              # single file, auto-detect type
  python main.py --pdf invoices/invoice.pdf --type VIAJES  # override type
  python main.py --pdf invoices/ --learn                 # learning mode: compare to *_truth.json files
  python main.py --list-types                            # show configured types

Learning mode:
  Place a <filename>_truth.json alongside each PDF. The agent processes normally,
  then compares its results to the ground truth and writes learnings to learnings.md.
  See invoices/example_truth.json for the expected format.
"""

import argparse
import csv
import json
import logging
import os
import re
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

from src.config.loader import ConfigStore, load_config
from src.llm.config_resolve import active_rule_groups_from_config
from src.agent.agent import InvoiceAgent
from src.agent.state import AgentState, rule_verdict_summary
from src.output.canonical_csv import write_workbook_csvs
from src.output.writer import write_results
from src.output.presenter import ConfigLoadSummary, NullPresenter, RunPresenter
from src.agent.agent_settings import clip_for_log, parse_agent_runtime_settings
from src.learning.evaluator import (
    evaluate,
    format_diff_for_agent,
    ground_truth_csv_configured,
    load_ground_truth,
)
from src.output.google_sheets import (
    GoogleSheetsOutputError,
    GoogleSheetsWorkbookWriter,
    load_workbook_tables_from_csv_dir,
    parse_google_sheets_target,
)
from src.output.workbook import (
    RAW_COMPLIANCE_RESULTS_TABLE,
    RAW_INVOICE_SUMMARY_TABLE,
    build_workbook_from_states,
)
from src.sources.local import (
    SourceError,
    is_folder_batch,
    legacy_local_output_dir,
    materialize_local_input,
)
from src.sources.google_drive import (
    GoogleDriveSourceError,
    build_google_drive_service,
    cleanup_materialized_google_drive_document,
    discover_google_drive_documents,
    google_drive_config_folder_enabled,
    google_drive_cleanup_enabled,
    google_drive_auth_mode,
    google_drive_output_dir,
    materialize_google_drive_config_folder,
    materialize_google_drive_document,
    resolve_google_drive_credentials,
    resolve_google_drive_folder_id,
)
from src.sources.models import RunIdentity, SourceProvenance
from src.tools.tools import reset_learnings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)

_LOG_LEVELS: dict[str, int] = {
    "CRITICAL": logging.CRITICAL,
    "ERROR": logging.ERROR,
    "WARNING": logging.WARNING,
    "WARN": logging.WARNING,
    "INFO": logging.INFO,
    "DEBUG": logging.DEBUG,
}

# Google APIs often accept ?key=... ; urllib3 DEBUG logs the full URL. Redact if anything slips through.
_URL_QUERY_KEY_RE = re.compile(r"([?&])key=[A-Za-z0-9_\-]+")


class _RedactUrlQueryKeyFilter(logging.Filter):
    """Strip API keys from log messages (e.g. urllib3 connection debug lines)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _URL_QUERY_KEY_RE.sub(r"\1key=***", record.msg)
        if record.args:
            record.args = tuple(
                _URL_QUERY_KEY_RE.sub(r"\1key=***", a) if isinstance(a, str) else a
                for a in record.args
            )
        return True


def _install_url_query_key_redaction() -> None:
    if getattr(_install_url_query_key_redaction, "_done", False):
        return
    f = _RedactUrlQueryKeyFilter()
    for name in ("urllib3.connectionpool", "urllib3.connection"):
        logging.getLogger(name).addFilter(f)
    setattr(_install_url_query_key_redaction, "_done", True)


_install_url_query_key_redaction()
logger = logging.getLogger("main")


def apply_configured_log_level(app_config: dict, *, presentation: bool = False) -> None:
    """
    Make logging level configurable via config/config.yaml.

    main.py initializes logging at import time to keep early prints consistent,
    but we override the level once we have loaded the YAML config.
    """
    if presentation:
        lvl = logging.WARNING
    else:
        lvl_raw = (app_config.get("logging", {}) or {}).get("level", "INFO")
        lvl = _LOG_LEVELS.get(str(lvl_raw).strip().upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(lvl)
    # Ensure existing handlers respect the updated level (basicConfig sets handler levels too).
    for h in list(root.handlers):
        try:
            h.setLevel(lvl)
        except Exception:
            pass


def presentation_enabled(app_config: dict, cli_flag: bool) -> bool:
    if cli_flag:
        return True
    return bool((app_config.get("logging", {}) or {}).get("presentation", False))


def _load_dotenv_files() -> None:
    """Load `.env` from repo root, then current working directory (later wins for duplicate keys)."""
    root = Path(__file__).resolve().parent
    load_dotenv(root / ".env")
    load_dotenv()


def load_app_config(path: str = "config/config.yaml") -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _count_allowed_value_sets(config_dir: str | Path) -> int:
    path = Path(config_dir) / "allowed_values.csv"
    if not path.exists():
        return 0
    pairs: set[tuple[str, str]] = set()
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            field_name = str(row.get("field_name", "")).strip()
            type_id = str(row.get("invoice_type_id", "")).strip()
            value = str(row.get("value", "")).strip()
            if field_name and value:
                pairs.add((field_name, type_id))
    return len(pairs)


def _config_summary(source: str, config_dir: str | Path, store) -> ConfigLoadSummary:
    return ConfigLoadSummary(
        source=source,
        path=str(config_dir),
        invoice_types=len(store.invoice_types),
        extraction_fields=sum(len(v) for v in store.extraction_fields.values()),
        compliance_rules=sum(len(v) for v in store.compliance_rules.values()),
        allowed_value_sets=_count_allowed_value_sets(config_dir),
        denylist_phrases=len(store.employee_name_role_denylist),
    )


def load_config_store(app_config: dict, *, force_local: bool = False):
    if force_local or not google_drive_config_folder_enabled(app_config):
        config_dir = app_config.get("config_dir", "config/csv")
        store = load_config(config_dir)
        return store, _config_summary("local config/csv", config_dir, store)

    try:
        config_dir = materialize_google_drive_config_folder(app_config)
    except GoogleDriveSourceError as e:
        print(str(e))
        sys.exit(1)
    store = load_config(str(config_dir))
    return store, _config_summary("Google Drive config folder", config_dir, store)


def process_invoice(
    agent: InvoiceAgent,
    pdf_path: str,
    output_dir: str,
    invoice_type_id: str = "",
    learn: bool = False,
    source_provenance: SourceProvenance | None = None,
    run_identity: RunIdentity | None = None,
):
    logger.info(f"Processing: {pdf_path}")
    state = agent.run(
        pdf_path=pdf_path,
        output_dir=output_dir,
        invoice_type_id=invoice_type_id,
        source_provenance=source_provenance,
        run_identity=run_identity,
    )
    paths = write_results(state, output_dir)

    presenter = getattr(agent, "presenter", NullPresenter())
    if not presenter.active:
        print(f"\n{'='*60}")
        print(f"  {Path(pdf_path).name}")
        print(f"  Status   : {state.status.value.upper()}")
        print(f"  Turns    : {state.turn}")
        print(f"  Fields   : {len(state.extracted_fields)} extracted")
        _rv = rule_verdict_summary(state.rule_results)
        print(
            f"  Rules    : {len(state.passed_rules)} passed | "
            f"blocking errors: {len(_rv['error_failed_rule_ids'])} | "
            f"warnings: {len(_rv['warning_failed_rule_ids'])}"
        )
        if _rv["error_failed_rule_ids"]:
            print(f"  Errors   : {', '.join(_rv['error_failed_rule_ids'])}")
        if _rv["warning_failed_rule_ids"]:
            print(f"  Warnings : {', '.join(_rv['warning_failed_rule_ids'])}")
        flagged = [k for k, v in state.extracted_fields.items() if v.flagged_for_review]
        if flagged:
            print(f"  Review   : {', '.join(flagged)}")
        print(f"  Output   : {paths['fields_csv']}")

    cfg = getattr(agent, "config", None) or {}
    agent_block = cfg.get("agent") or {}
    date_parse = str(agent_block.get("ground_truth_date_parse", "DMY")).strip().upper()
    effective_source_provenance = state.source_provenance or source_provenance
    truth = load_ground_truth(
        pdf_path,
        config=cfg,
        invoice_type_id=state.invoice_type_id or None,
        source_provenance=effective_source_provenance,
    )
    _last_score: dict | None = None
    _last_field_results: dict | None = None
    learn_ran = False
    no_ground_truth = False
    ground_truth_csv_only = False
    if truth is not None:
        diff = evaluate(state, truth, store=agent.store, date_parse=date_parse)
        _last_score = diff["score"]
        _last_field_results = diff["field_results"]
        diff_text = format_diff_for_agent(diff)
        s = diff["score"]
        ft = s.get("fields_total") or 0
        if not presenter.active:
            if ft:
                print(
                    f"  Ground truth: {ft} field(s) compared | "
                    f"exact {s.get('fields_exact', 0)} · partial {s.get('fields_partial', 0)} · "
                    f"wrong {s.get('fields_wrong', 0)} "
                    f"(missing {s.get('fields_not_extracted', 0)} · mismatch {s.get('fields_wrong_value', 0)})"
                )
                if s.get("field_accuracy") is not None:
                    exa = s.get("exact_accuracy")
                    print(
                        f"                 lenient {s['field_accuracy']:.0%} (exact + partial)  "
                        f"· strict {(exa if exa is not None else 0):.0%} (exact only)"
                    )
            else:
                print("  Ground truth: no overlapping fields to score")
            if s.get("rule_accuracy") is not None:
                rt = s.get("rules_total") or 0
                print(
                    f"                 compliance vs truth: {s.get('rules_correct', 0)}/{rt} "
                    f"({s['rule_accuracy']:.0%})"
                )
        rt = parse_agent_runtime_settings(cfg)
        max_c = int(rt.get("log_line_max_chars", 120))
        for line in diff_text.splitlines():
            logger.info(clip_for_log(line, max_c))
        if learn:
            if not presenter.active:
                print(f"  Learn    : running reflection loop...")
            agent.run_reflection(state, diff_text)
            learn_ran = True
            if not presenter.active:
                print(f"             Learnings written to learnings.md")
    else:
        if learn:
            stem = Path(pdf_path).stem
            no_ground_truth = True
            if not presenter.active:
                print(f"  Learn    : no ground truth — skipping reflection")
                print(f"             (add {stem}_truth.json or a matching CSV row)")
        elif ground_truth_csv_configured(cfg):
            ground_truth_csv_only = True
            if not presenter.active:
                print(f"  Ground truth: none for this file (no JSON, no CSV row matched)")

    if presenter.active:
        presenter.run_complete(
            state,
            paths=paths,
            ground_truth_score=_last_score,
            learn=learn,
            learn_ran=learn_ran,
            no_ground_truth=no_ground_truth,
            ground_truth_csv_only=ground_truth_csv_only,
        )
    else:
        print(f"{'='*60}\n")
    return state, _last_score, _last_field_results


def _print_field_accuracy(field_results_list: list[dict]) -> None:
    """Print a per-field accuracy breakdown aggregated across all scored invoices."""
    from collections import defaultdict

    stats: dict[str, dict] = defaultdict(lambda: {"compared": 0, "exact": 0, "partial": 0, "wrong": 0})
    for fr in field_results_list:
        for fname, r in fr.items():
            if r.get("match") is None:
                continue
            stats[fname]["compared"] += 1
            if r["match"] is True:
                stats[fname]["exact"] += 1
            elif r.get("partial"):
                stats[fname]["partial"] += 1
            else:
                stats[fname]["wrong"] += 1

    if not stats:
        return

    COL_FIELD = max(24, max(len(f) for f in stats) + 2)
    header = f"{'Field':<{COL_FIELD}}{'Compared':>10}{'Exact':>7}{'Partial':>9}{'Wrong':>7}{'Strict%':>9}"
    sep = "-" * len(header)
    print(f"\n{'='*len(header)}\n  PER-FIELD ACCURACY\n{'='*len(header)}")
    print(header)
    print(sep)

    for fname in sorted(stats, key=lambda f: stats[f]["exact"] / stats[f]["compared"] if stats[f]["compared"] else 1):
        s = stats[fname]
        n = s["compared"]
        acc = f"{s['exact'] / n * 100:.0f}%" if n else "—"
        print(f"{fname:<{COL_FIELD}}{n:>10}{s['exact']:>7}{s['partial']:>9}{s['wrong']:>7}{acc:>9}")

    print(f"{'='*len(header)}\n")


def _print_batch_summary(
    results: list[tuple[str, str, dict | None]],
    field_results_list: list[dict] | None = None,
    csv_path: str | None = None,
) -> None:
    COL_FILE = max(30, max(len(r[0]) for r in results) + 2)
    COL_TYPE, COL_TOTAL, COL_EXACT, COL_PARTIAL, COL_WRONG = 16, 7, 7, 9, 7
    COL_LENIENT, COL_STRICT = 12, 10

    header = (
        f"{'File':<{COL_FILE}}{'Type':<{COL_TYPE}}"
        f"{'Total':>{COL_TOTAL}}{'Exact':>{COL_EXACT}}{'Partial':>{COL_PARTIAL}}"
        f"{'Wrong':>{COL_WRONG}}{'Lenient%':>{COL_LENIENT}}{'Strict%':>{COL_STRICT}}"
    )
    sep = "-" * len(header)
    print(f"\n{'='*len(header)}\n  BATCH ACCURACY SUMMARY\n{'='*len(header)}")
    print(header)
    print(sep)

    agg_total = agg_exact = agg_partial = agg_wrong = scored_count = 0
    csv_rows = []

    for filename, type_id, s in results:
        if s is None or not s.get("fields_total"):
            print(
                f"{filename:<{COL_FILE}}{(type_id or '?'):<{COL_TYPE}}"
                f"{'—':>{COL_TOTAL}}{'—':>{COL_EXACT}}{'—':>{COL_PARTIAL}}"
                f"{'—':>{COL_WRONG}}{'—':>{COL_LENIENT}}{'—':>{COL_STRICT}}"
            )
            csv_rows.append({
                "filename": filename, "invoice_type": type_id or "",
                "fields_total": "", "fields_exact": "", "fields_partial": "",
                "fields_wrong": "", "field_accuracy_pct": "", "exact_accuracy_pct": "",
            })
        else:
            ft = s["fields_total"]
            fe = s["fields_exact"]
            fp = s["fields_partial"]
            fw = s["fields_wrong"]
            la = s.get("field_accuracy")
            ea = s.get("exact_accuracy")
            la_s = f"{la*100:.0f}%" if la is not None else "—"
            ea_s = f"{ea*100:.0f}%" if ea is not None else "—"
            print(
                f"{filename:<{COL_FILE}}{(type_id or '?'):<{COL_TYPE}}"
                f"{ft:>{COL_TOTAL}}{fe:>{COL_EXACT}}{fp:>{COL_PARTIAL}}"
                f"{fw:>{COL_WRONG}}{la_s:>{COL_LENIENT}}{ea_s:>{COL_STRICT}}"
            )
            csv_rows.append({
                "filename": filename, "invoice_type": type_id or "",
                "fields_total": ft, "fields_exact": fe, "fields_partial": fp,
                "fields_wrong": fw,
                "field_accuracy_pct": f"{la*100:.1f}" if la is not None else "",
                "exact_accuracy_pct": f"{ea*100:.1f}" if ea is not None else "",
            })
            agg_total += ft
            agg_exact += fe
            agg_partial += fp
            agg_wrong += fw
            scored_count += 1

    print(sep)
    if agg_total:
        agg_la = (agg_exact + agg_partial) / agg_total
        agg_ea = agg_exact / agg_total
        print(
            f"{'TOTALS':<{COL_FILE}}{f'({scored_count} scored)':<{COL_TYPE}}"
            f"{agg_total:>{COL_TOTAL}}{agg_exact:>{COL_EXACT}}{agg_partial:>{COL_PARTIAL}}"
            f"{agg_wrong:>{COL_WRONG}}{f'{agg_la*100:.0f}%':>{COL_LENIENT}}{f'{agg_ea*100:.0f}%':>{COL_STRICT}}"
        )
    else:
        print(f"{'TOTALS':<{COL_FILE}}  (no ground truth rows matched any PDF)")
    print(f"{'='*len(header)}\n")

    if csv_path:
        import csv as _csv
        fieldnames = [
            "filename", "invoice_type", "fields_total", "fields_exact",
            "fields_partial", "fields_wrong", "field_accuracy_pct", "exact_accuracy_pct",
        ]
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = _csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(csv_rows)
        print(f"  Batch summary written to: {csv_path}\n")

    if field_results_list:
        _print_field_accuracy(field_results_list)


def _write_batch_workbook_outputs(
    states: list[AgentState],
    output_dir: str | Path,
    app_config: dict,
    store: ConfigStore | None = None,
) -> None:
    successful_states = list(states)
    if not successful_states:
        return

    active_rule_groups = active_rule_groups_from_config(app_config)
    rule_metadata = []
    if store is not None and hasattr(store, "get_rules"):
        invoice_types = sorted({state.invoice_type_id for state in successful_states if state.invoice_type_id})
        rule_metadata = [
            rule
            for invoice_type_id in invoice_types
            for rule in store.get_rules(invoice_type_id, active_rule_groups)
        ]

    if rule_metadata:
        tables = build_workbook_from_states(successful_states, rule_metadata)
    else:
        tables = build_workbook_from_states(successful_states)
    workbook_output_dir = Path(output_dir) / "canonical_workbook"
    csv_paths = write_workbook_csvs(tables, workbook_output_dir)

    print("Canonical workbook CSVs written.")
    print(f"  Directory     : {workbook_output_dir}")
    print(f"  Managed tables: {len(csv_paths)} table(s)")
    if csv_paths:
        print(f"  Table names   : {', '.join(csv_paths.keys())}")

    target = parse_google_sheets_target(app_config)
    if not target.enabled:
        return

    sheets_tables = tables
    if not target.include_generated_views:
        raw_table_names = {RAW_INVOICE_SUMMARY_TABLE, RAW_COMPLIANCE_RESULTS_TABLE}
        sheets_tables = [table for table in tables if table.name in raw_table_names]

    result = GoogleSheetsWorkbookWriter(app_config=app_config).write_workbook(sheets_tables, target)
    print("Google Sheets workbook sync complete.")
    print(f"  Spreadsheet ID : {result.spreadsheet_id}")
    print(f"  Spreadsheet URL: {result.spreadsheet_url}")
    print(f"  Managed tabs   : {len(result.managed_tabs)} managed tab(s)")
    if result.managed_tabs:
        print(f"  Tab names      : {', '.join(result.managed_tabs)}")
    print(f"  Updated cells  : {result.updated_cells}")


def main():
    parser = argparse.ArgumentParser(description="Invoice Compliance Agent")
    parser.add_argument("--pdf", default=None, help="Path to PDF file or folder of PDFs (default: invoices/)")
    parser.add_argument("--google-drive-folder-id", default=None, help="Google Drive folder ID to process PDFs from")
    parser.add_argument("--drive-auth", action="store_true", help="Authenticate Google Drive OAuth and save token")
    parser.add_argument(
        "--drive-oauth-client-secret",
        default=None,
        metavar="PATH",
        help="Path to Google Drive OAuth client JSON (default: config/env)",
    )
    parser.add_argument("--type", help="Invoice type ID (e.g. EU_VAT, DE_INVOICE)")
    parser.add_argument("--output", default="output", help="Output directory (default: output/)")
    parser.add_argument(
        "--config",
        default=os.environ.get("CONFIG_PATH", "config/config.yaml"),
        help="Config file path",
    )
    parser.add_argument(
        "--local-config",
        "--no-drive-config",
        dest="local_config",
        action="store_true",
        help="Load config CSVs from local config_dir even when Google Drive config_folder is enabled",
    )
    parser.add_argument("--list-types", action="store_true", help="List available invoice types")
    parser.add_argument("--learn", action="store_true",
                        help="Learning mode: after processing, compare to *_truth.json and write learnings")
    parser.add_argument(
        "--reset-learnings", nargs="*", metavar="ARG",
        help=(
            "Reset learnings (backs up to .bak first). "
            "No args: wipe all. "
            "One arg: wipe a type (e.g. VIAJES). "
            "Two args: wipe a category within a type (e.g. VIAJES extraction_patterns)."
        ),
    )
    parser.add_argument("--yes", "-y", action="store_true",
                        help="Skip confirmation prompt (use with --reset-learnings)")
    parser.add_argument(
        "--batch-summary-csv", default=None, metavar="PATH",
        help="Write batch accuracy summary to CSV file (batch mode only)",
    )
    parser.add_argument(
        "--presentation",
        action="store_true",
        help="Live demo mode: Rich-formatted phase-aware output (suppresses INFO logs)",
    )
    parser.add_argument(
        "--upload-workbook-csv-dir",
        default=None,
        metavar="PATH",
        help="Upload an existing canonical workbook CSV directory to Google Sheets without processing invoices",
    )
    parser.add_argument(
        "--sheets-spreadsheet-id",
        default=None,
        metavar="ID",
        help="Google Sheets spreadsheet ID for --upload-workbook-csv-dir",
    )
    parser.add_argument(
        "--sheets-create-title",
        default=None,
        metavar="TITLE",
        help="Create a new Google Sheets spreadsheet with this title for --upload-workbook-csv-dir",
    )
    args = parser.parse_args()
    _load_dotenv_files()

    if args.pdf and args.google_drive_folder_id:
        parser.error("--pdf and --google-drive-folder-id cannot be used together")

    # --reset-learnings handling (runs before anything else, no agent needed)
    if args.reset_learnings is not None:
        reset_args = args.reset_learnings  # list of 0, 1, or 2 items
        type_id  = reset_args[0] if len(reset_args) > 0 else ""
        category = reset_args[1] if len(reset_args) > 1 else ""

        app_config_early = load_app_config(args.config)
        apply_configured_log_level(
            app_config_early,
            presentation=presentation_enabled(app_config_early, args.presentation),
        )
        learnings_path = app_config_early.get("learnings_path", "learnings/learnings.md")

        # Describe what we're about to delete
        if not type_id:
            scope = "ALL learnings"
        elif not category:
            scope = f"all learnings for type '{type_id}'"
        else:
            scope = f"category '{category}' under type '{type_id}'"

        print(f"\nAbout to delete {scope} from {learnings_path}")
        if not args.yes:
            answer = input("Confirm? [y/N] ").strip().lower()
            if answer != "y":
                print("Aborted.")
                sys.exit(0)

        result = reset_learnings(
            learnings_path=learnings_path,
            invoice_type_id=type_id,
            category=category,
        )

        if result.get("removed", 0) == 0:
            print(f"Nothing removed. {result.get('note', '')}")
        else:
            print(f"Removed {result['removed']} learning(s). Backup saved to {result['backup']}")
        sys.exit(0)

    app_config = load_app_config(args.config)
    pres_on = presentation_enabled(app_config, args.presentation)
    apply_configured_log_level(app_config, presentation=pres_on)

    if args.upload_workbook_csv_dir:
        try:
            target = parse_google_sheets_target(
                app_config,
                {
                    "enabled": True,
                    "spreadsheet_id": args.sheets_spreadsheet_id,
                    "create_title": args.sheets_create_title,
                },
            )
            tables = load_workbook_tables_from_csv_dir(args.upload_workbook_csv_dir)
            result = GoogleSheetsWorkbookWriter(app_config=app_config).write_workbook(tables, target)
        except GoogleSheetsOutputError as e:
            print(str(e))
            sys.exit(1)

        print("Google Sheets fixture upload complete.")
        print(f"  Spreadsheet ID : {result.spreadsheet_id}")
        print(f"  Spreadsheet URL: {result.spreadsheet_url}")
        print(f"  Managed tabs   : {len(result.managed_tabs)} managed tab(s)")
        if result.managed_tabs:
            print(f"  Tab names      : {', '.join(result.managed_tabs)}")
        print(f"  Updated cells  : {result.updated_cells}")
        return

    if args.drive_auth:
        try:
            drive_auth_mode = google_drive_auth_mode(app_config)
            creds = resolve_google_drive_credentials(
                app_config,
                oauth_client_secret_path=args.drive_oauth_client_secret,
                force_interactive=True,
            )
        except GoogleDriveSourceError as e:
            print(str(e))
            sys.exit(1)
        drive_cfg = ((app_config or {}).get("sources") or {}).get("google_drive") or {}
        if drive_auth_mode == "service_account":
            identity = (
                getattr(creds, "service_account_email", None)
                or getattr(creds, "client_email", None)
                or drive_cfg.get("client_email")
            )
            granted = sorted(getattr(creds, "scopes", None) or drive_cfg.get("scopes", []) or [])
            print("Google Drive service-account credentials validated.")
            if identity:
                print(f"  Identity: {identity}")
            if granted:
                print(f"  Scopes  : {', '.join(granted)}")
            return

        token_path = drive_cfg.get("token_path", ".secrets/google-drive-token.json")
        granted = sorted(getattr(creds, "scopes", None) or drive_cfg.get("scopes", []) or [])
        print("Google Drive OAuth token saved.")
        print(f"  Token : {token_path}")
        if granted:
            print(f"  Scopes: {', '.join(granted)}")
        return

    logging_cfg = app_config.get("logging", {}) or {}
    presenter = (
        RunPresenter(show_reasoning=bool(logging_cfg.get("presentation_show_reasoning", False)))
        if pres_on else NullPresenter()
    )
    store, config_summary = load_config_store(app_config, force_local=args.local_config)

    if args.list_types:
        if presenter.active:
            presenter.startup_context(config_summary, app_config, ocr_enabled=None)
        print("\nConfigured invoice types:")
        for tid, t in store.invoice_types.items():
            fields = len(store.get_fields(tid))
            rules = len(store.get_rules(tid))
            print(f"  {tid:20s}  {t.display_name}  ({fields} fields, {rules} rules)")
        return


    if args.type and args.type not in store.invoice_types:
        print(f"Unknown invoice type: {args.type}")
        print(f"Available: {', '.join(store.invoice_types.keys())}")
        sys.exit(1)

    drive_folder_id = ""
    if not args.pdf:
        try:
            drive_folder_id = resolve_google_drive_folder_id(app_config, override=args.google_drive_folder_id)
        except GoogleDriveSourceError as e:
            if args.google_drive_folder_id:
                print(str(e))
                sys.exit(1)
            drive_folder_id = ""

    using_google_drive = bool(drive_folder_id)
    drive_refs = []
    drive_service = None
    if using_google_drive:
        try:
            drive_creds = resolve_google_drive_credentials(
                app_config,
                oauth_client_secret_path=args.drive_oauth_client_secret,
            )
            drive_service = build_google_drive_service(drive_creds, app_config)
            drive_refs = discover_google_drive_documents(
                drive_folder_id,
                app_config,
                service=drive_service,
            )
        except GoogleDriveSourceError as e:
            print(str(e))
            sys.exit(1)
        if not drive_refs:
            print(f"No PDF files found in Google Drive folder: {drive_folder_id}")
            return
        materialized_docs = []
    else:
        pdf_input = args.pdf or "invoices/"
        try:
            materialized_docs = materialize_local_input(pdf_input)
        except SourceError as e:
            print(str(e))
            sys.exit(1)

    agent = InvoiceAgent(config=app_config, store=store, presenter=presenter)
    if presenter.active:
        presenter.startup_context(
            config_summary,
            app_config,
            ocr_enabled=getattr(agent, "surya_models", None) is not None,
        )

    if using_google_drive:
        logger.info(f"Batch mode: {len(drive_refs)} PDFs found in Google Drive folder {drive_folder_id}")
        if presenter.active:
            presenter.batch_drive_start(len(drive_refs), drive_folder_id)
        else:
            print(f"Google Drive: found {len(drive_refs)} PDF(s) in folder {drive_folder_id}", flush=True)
        failed_pdfs = []
        batch_results: list[tuple[str, str, dict | None]] = []
        batch_field_results: list[dict] = []
        successful_states: list[AgentState] = []
        for idx, ref in enumerate(drive_refs, start=1):
            doc = None
            try:
                if presenter.active:
                    presenter.batch_drive_item(idx, len(drive_refs), ref.display_name)
                else:
                    print(f"Google Drive: processing {idx}/{len(drive_refs)} — {ref.display_name}", flush=True)
                doc = materialize_google_drive_document(ref, app_config, service=drive_service)
                pdf = Path(doc.local_pdf_path)
                out_dir = google_drive_output_dir(doc, args.output)
                state, score, field_results = process_invoice(
                    agent,
                    doc.local_pdf_path,
                    str(out_dir),
                    invoice_type_id=args.type or "",
                    learn=args.learn,
                    source_provenance=doc.provenance,
                    run_identity=doc.run_identity,
                )
                batch_results.append((ref.display_name, state.invoice_type_id or "", score))
                successful_states.append(state)
                if field_results:
                    batch_field_results.append(field_results)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                logger.error(f"Failed to process {ref.display_name}: {e}", exc_info=True)
                failed_pdfs.append((ref.display_name, str(e)))
                batch_results.append((ref.display_name, "", None))
            finally:
                if doc is not None and google_drive_cleanup_enabled(app_config):
                    cleanup_materialized_google_drive_document(doc)
        if failed_pdfs:
            print(f"\n{'='*60}")
            print(f"  Batch complete — {len(failed_pdfs)} PDF(s) failed:")
            for name, err in failed_pdfs:
                print(f"    ✗ {name}: {err}")
            print(f"{'='*60}\n")
        try:
            _write_batch_workbook_outputs(successful_states, args.output, app_config, store)
        except GoogleSheetsOutputError as e:
            print(str(e))
            sys.exit(1)
        if batch_results and any(s is not None for _, _, s in batch_results):
            _print_batch_summary(
                batch_results,
                field_results_list=batch_field_results or None,
                csv_path=args.batch_summary_csv,
            )
    elif is_folder_batch(materialized_docs):
        batch_label = (
            args.pdf or "invoices/"
        )
        logger.info(f"Batch mode: {len(materialized_docs)} PDFs found in {batch_label}")
        failed_pdfs = []
        batch_results: list[tuple[str, str, dict | None]] = []
        batch_field_results: list[dict] = []
        successful_states: list[AgentState] = []
        for doc in materialized_docs:
            pdf = Path(doc.local_pdf_path)
            out_dir = legacy_local_output_dir(doc, args.output)
            try:
                state, score, field_results = process_invoice(
                    agent,
                    doc.local_pdf_path,
                    str(out_dir),
                    invoice_type_id=args.type or "",
                    learn=args.learn,
                    source_provenance=doc.provenance,
                    run_identity=doc.run_identity,
                )
                batch_results.append((pdf.name, state.invoice_type_id or "", score))
                successful_states.append(state)
                if field_results:
                    batch_field_results.append(field_results)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                logger.error(f"Failed to process {pdf.name}: {e}", exc_info=True)
                failed_pdfs.append((pdf.name, str(e)))
                batch_results.append((pdf.name, "", None))
        if failed_pdfs:
            print(f"\n{'='*60}")
            print(f"  Batch complete — {len(failed_pdfs)} PDF(s) failed:")
            for name, err in failed_pdfs:
                print(f"    ✗ {name}: {err}")
            print(f"{'='*60}\n")
        try:
            _write_batch_workbook_outputs(successful_states, args.output, app_config, store)
        except GoogleSheetsOutputError as e:
            print(str(e))
            sys.exit(1)
        if batch_results and any(s is not None for _, _, s in batch_results):
            _print_batch_summary(
                batch_results,
                field_results_list=batch_field_results or None,
                csv_path=args.batch_summary_csv,
            )
    else:
        doc = materialized_docs[0]
        out_dir = legacy_local_output_dir(doc, args.output)
        process_invoice(
            agent,
            doc.local_pdf_path,
            str(out_dir),
            invoice_type_id=args.type or "",
            learn=args.learn,
            source_provenance=doc.provenance,
            run_identity=doc.run_identity,
        )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(1)
