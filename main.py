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
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

from src.config.loader import load_config
from src.agent.agent import InvoiceAgent
from src.output.writer import write_results
from src.learning.evaluator import load_ground_truth, evaluate, format_diff_for_agent
from src.tools.tools import reset_learnings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("main")

_LOG_LEVELS: dict[str, int] = {
    "CRITICAL": logging.CRITICAL,
    "ERROR": logging.ERROR,
    "WARNING": logging.WARNING,
    "WARN": logging.WARNING,
    "INFO": logging.INFO,
    "DEBUG": logging.DEBUG,
}


def apply_configured_log_level(app_config: dict) -> None:
    """
    Make logging level configurable via config/config.yaml.

    main.py initializes logging at import time to keep early prints consistent,
    but we override the level once we have loaded the YAML config.
    """
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

    print(f"\n{'='*60}")
    print(f"  {Path(pdf_path).name}")
    print(f"  Status   : {state.status.value.upper()}")
    print(f"  Turns    : {state.turn}")
    print(f"  Fields   : {len(state.extracted_fields)} extracted")
    print(f"  Rules    : {len(state.passed_rules)} passed / {len(state.failed_rules)} failed")
    if state.failed_rules:
        print(f"  Failed   : {', '.join(state.failed_rules)}")
    flagged = [k for k, v in state.extracted_fields.items() if v.flagged_for_review]
    if flagged:
        print(f"  Review   : {', '.join(flagged)}")
    print(f"  Output   : {paths['fields_csv']}")

    if learn:
        truth = load_ground_truth(pdf_path, config=getattr(agent, "config", None))
        if truth is None:
            print(f"  Learn    : no truth file found — skipping reflection")
            print(f"             (expected: {Path(pdf_path).stem}_truth.json)")
        else:
            diff = evaluate(state, truth)
            diff_text = format_diff_for_agent(diff)
            s = diff["score"]
            print(f"  Learn    : field accuracy {s.get('field_accuracy', '?') or '?'} | "
                  f"rule accuracy {s.get('rule_accuracy', '?') or '?'}")
            print(f"             Running reflection loop...")
            agent.run_reflection(state, diff_text)
            print(f"             Learnings written to learnings.md")

    print(f"{'='*60}\n")
    return state


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
    args = parser.parse_args()
    _load_dotenv_files()

    # --reset-learnings handling (runs before anything else, no agent needed)
    if args.reset_learnings is not None:
        reset_args = args.reset_learnings  # list of 0, 1, or 2 items
        type_id  = reset_args[0] if len(reset_args) > 0 else ""
        category = reset_args[1] if len(reset_args) > 1 else ""

        app_config_early = load_app_config(args.config)
        apply_configured_log_level(app_config_early)
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
    apply_configured_log_level(app_config)
    store = load_config(app_config.get("config_dir", "config/csv"))

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

    agent = InvoiceAgent(config=app_config, store=store)

    pdf_path = Path(args.pdf)
    if pdf_path.is_dir():
        pdfs = sorted(pdf_path.glob("*.pdf"))
        if not pdfs:
            print(f"No PDFs found in {args.pdf}")
            sys.exit(1)
        logger.info(f"Batch mode: {len(pdfs)} PDFs found in {pdf_path}")
        failed_pdfs = []
        for pdf in pdfs:
            out_dir = Path(args.output) / pdf.stem
            try:
                process_invoice(agent, str(pdf), str(out_dir), invoice_type_id=args.type or "", learn=args.learn)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                logger.error(f"Failed to process {pdf.name}: {e}", exc_info=True)
                failed_pdfs.append((pdf.name, str(e)))
        if failed_pdfs:
            print(f"\n{'='*60}")
            print(f"  Batch complete — {len(failed_pdfs)} PDF(s) failed:")
            for name, err in failed_pdfs:
                print(f"    ✗ {name}: {err}")
            print(f"{'='*60}\n")
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
