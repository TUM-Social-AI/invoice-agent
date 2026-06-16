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
import json
import logging
import re
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

from src.config.loader import load_config
from src.agent.agent import InvoiceAgent
from src.agent.state import rule_verdict_summary
from src.output.writer import write_results
from src.output.presenter import NullPresenter, RunPresenter
from src.agent.agent_settings import clip_for_log, parse_agent_runtime_settings
from src.learning.evaluator import (
    evaluate,
    format_diff_for_agent,
    ground_truth_csv_configured,
    load_ground_truth,
)
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


def process_invoice(
    agent: InvoiceAgent,
    pdf_path: str,
    output_dir: str,
    invoice_type_id: str = "",
    learn: bool = False,
):
    logger.info(f"Processing: {pdf_path}")
    state = agent.run(
        pdf_path=pdf_path,
        output_dir=output_dir,
        invoice_type_id=invoice_type_id,
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
    truth = load_ground_truth(
        pdf_path,
        config=cfg,
        invoice_type_id=state.invoice_type_id or None,
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


def main():
    parser = argparse.ArgumentParser(description="Invoice Compliance Agent")
    parser.add_argument("--pdf", default="invoices/", help="Path to PDF file or folder of PDFs (default: invoices/)")
    parser.add_argument("--type", help="Invoice type ID (e.g. EU_VAT, DE_INVOICE)")
    parser.add_argument("--output", default="output", help="Output directory (default: output/)")
    parser.add_argument("--config", default="config/config.yaml", help="Config file path")
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
    args = parser.parse_args()
    _load_dotenv_files()

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
    store = load_config(app_config.get("config_dir", "config/csv"))

    presenter = RunPresenter() if pres_on else NullPresenter()

    if args.list_types:
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

    agent = InvoiceAgent(config=app_config, store=store, presenter=presenter)

    pdf_path = Path(args.pdf)
    if pdf_path.is_dir():
        pdfs = sorted(pdf_path.glob("*.pdf"))
        if not pdfs:
            print(f"No PDFs found in {args.pdf}")
            sys.exit(1)
        logger.info(f"Batch mode: {len(pdfs)} PDFs found in {pdf_path}")
        failed_pdfs = []
        batch_results: list[tuple[str, str, dict | None]] = []
        batch_field_results: list[dict] = []
        for pdf in pdfs:
            out_dir = Path(args.output) / pdf.stem
            try:
                state, score, field_results = process_invoice(
                    agent, str(pdf), str(out_dir),
                    invoice_type_id=args.type or "", learn=args.learn,
                )
                batch_results.append((pdf.name, state.invoice_type_id or "", score))
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
        if batch_results:
            _print_batch_summary(
                batch_results,
                field_results_list=batch_field_results or None,
                csv_path=args.batch_summary_csv,
            )
    elif pdf_path.is_file():
        out_dir = Path(args.output) / pdf_path.stem
        process_invoice(agent, str(pdf_path), str(out_dir), invoice_type_id=args.type or "", learn=args.learn)
    else:
        print(f"PDF not found: {args.pdf}")
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(1)
