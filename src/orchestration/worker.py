"""
Poll-driven worker: the container entrypoint for the scheduled AWS task.

One invocation = one poll: list new documents from the source, claim each (so
overlapping polls and crashed tasks are safe), run the agent in-process (models loaded
once for the whole batch), upload results, and record terminal state. Then exit — the
schedule (EventBridge) fires the next poll.

Run locally with the filesystem stand-in:
    DRIVE_CLIENT=local LOCAL_INPUT_DIR=invoices LOCAL_OUTPUT_DIR=output/drive \
    CONFIG_PATH=config/config.yaml python -m src.orchestration.worker
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass

from src.orchestration.config import OrchestrationConfig
from src.orchestration.dedup import DedupStore, DynamoDbDedupStore, InMemoryDedupStore
from src.orchestration.drive_source import DriveClient
from src.sources.models import DocumentRef

logger = logging.getLogger("orchestration.worker")


@dataclass
class PollSummary:
    scanned: int = 0        # candidates listed from the input folder
    claimed: int = 0        # successfully claimed this poll
    processed_ok: int = 0   # completed, clean
    needs_review: int = 0   # completed, flagged for a human
    failed: int = 0         # raised; recorded failed/parked for retry
    skipped: int = 0        # not claimable (done / in-progress / parked)

    def as_dict(self) -> dict:
        return asdict(self)


def dedup_key(ref: DocumentRef) -> str:
    """Stable per-revision key: a replaced file (new revision) is reprocessed; an unchanged one is skipped."""
    source_id = ref.source_id or ref.uri
    revision = ref.revision_id or "norev"
    return f"{source_id}#{revision}"


def run_poll(
    agent,
    drive: DriveClient,
    store: DedupStore,
    cfg: OrchestrationConfig,
    *,
    process_fn=None,
) -> PollSummary:
    """
    Execute a single poll. `process_fn(agent, materialized, output_root) -> ProcessOutcome`
    defaults to the real agent runner; tests inject a fake to avoid loading the ML stack.
    """
    if process_fn is None:
        from src.orchestration.agent_runner import process_document as process_fn

    run_id = uuid.uuid4().hex
    summary = PollSummary()

    refs = drive.list_input_documents()
    summary.scanned = len(refs)
    if summary.scanned > cfg.max_files_per_poll:
        logger.warning(
            "poll found %d candidates; capping at max_files_per_poll=%d (rest picked up next poll)",
            summary.scanned, cfg.max_files_per_poll,
        )

    for ref in refs[: cfg.max_files_per_poll]:
        key = dedup_key(ref)
        decision = store.try_claim(
            key,
            run_id=run_id,
            now=time.time(),
            max_attempts=cfg.max_attempts,
            stale_seconds=cfg.stale_claim_seconds,
            display_name=ref.display_name,
        )
        if not decision.claimable:
            summary.skipped += 1
            logger.info("skip %s (%s)", ref.display_name, decision.reason)
            continue

        summary.claimed += 1
        materialized = None
        try:
            materialized = drive.materialize(ref)
            outcome = process_fn(agent, materialized, cfg.output_root)
            drive.upload_results(ref, outcome.result_paths, status=outcome.status)
            store.mark_done(key, run_id=run_id, needs_review=outcome.needs_review)
            if outcome.needs_review:
                summary.needs_review += 1
                logger.warning(
                    "needs_review %s type=%s blocking=%s flagged=%s",
                    ref.display_name, outcome.invoice_type_id,
                    outcome.blocking_rule_ids, outcome.flagged_fields,
                )
            else:
                summary.processed_ok += 1
                logger.info("done %s type=%s", ref.display_name, outcome.invoice_type_id)
        except Exception as e:  # noqa: BLE001 - any failure is recorded and retried/parked
            summary.failed += 1
            store.mark_failed(key, run_id=run_id, error=repr(e), max_attempts=cfg.max_attempts)
            logger.exception("failed %s: %s", ref.display_name, e)
        finally:
            if materialized is not None:
                try:
                    drive.cleanup(materialized)
                except Exception:  # noqa: BLE001
                    logger.warning("cleanup failed for %s", ref.display_name)

    logger.info("poll complete %s", summary.as_dict())
    return summary


def build_store(cfg: OrchestrationConfig) -> DedupStore:
    if cfg.dedup_backend == "memory":
        return InMemoryDedupStore()
    return DynamoDbDedupStore(cfg.dynamodb_table, region=cfg.aws_region)


def build_drive_client(app_config: dict) -> DriveClient:
    """
    Resolve the document source.

    DRIVE_CLIENT=local -> filesystem stand-in (LOCAL_INPUT_DIR / LOCAL_OUTPUT_DIR),
    which runs the whole loop with no AWS and no Drive credentials.

    Otherwise -> the real Google Drive source. NOTE: the google-drive-source branch
    exposes module-level *functions* (discover_google_drive_documents,
    materialize_google_drive_document, cleanup_materialized_google_drive_document,
    resolve_google_drive_folder_id) — not a client class — and still needs
    service-account auth + a result-upload function before it can back this worker.
    The adapter that wraps those functions into a DriveClient lands as a follow-up once
    that branch merges; until then this path is intentionally unimplemented.
    """
    mode = os.environ.get("DRIVE_CLIENT", "").strip().lower()
    if mode == "local":
        from src.orchestration.drive_source import LocalDirDriveClient

        return LocalDirDriveClient(
            os.environ.get("LOCAL_INPUT_DIR", "invoices"),
            os.environ.get("LOCAL_OUTPUT_DIR", "output/drive"),
        )

    # TODO(drive-adapter): implement a GoogleDriveAdapter(DriveClient) over the
    # google-drive-source module functions once that branch merges with service-account
    # auth and an upload-results-to-Drive function. It will take `app_config` (the Drive
    # functions resolve the folder via resolve_google_drive_folder_id(app_config)).
    raise NotImplementedError(
        "The Google Drive source adapter is not implemented yet. Set DRIVE_CLIENT=local "
        "for local runs. The production adapter (wrapping src.sources.google_drive's "
        "discover / materialize / cleanup / upload functions) lands once the "
        "google-drive-source branch merges with service-account auth and result upload."
    )


def main() -> None:
    import yaml

    from src.config.loader import load_config
    from src.agent.agent import InvoiceAgent

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )

    config_path = os.environ.get("CONFIG_PATH", "config/config.yaml")
    with open(config_path, encoding="utf-8") as f:
        app_config = yaml.safe_load(f)

    cfg = OrchestrationConfig.from_app_config(app_config)
    store = load_config(app_config.get("config_dir", "config/csv"))
    agent = InvoiceAgent(config=app_config, store=store)

    drive = build_drive_client(app_config)
    dedup = build_store(cfg)

    logger.info(
        "starting poll: backend=%s table=%s cap=%d",
        cfg.dedup_backend, cfg.dynamodb_table, cfg.max_files_per_poll,
    )
    run_poll(agent, drive, dedup, cfg)


if __name__ == "__main__":
    main()
