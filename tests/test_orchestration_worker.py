"""
End-to-end orchestration tests using the local filesystem Drive stand-in and the
in-memory dedup store, with a fake agent runner (no ML stack / no LLM).

Covers the guarantees that matter for deployment:
  - a clean file is processed once and skipped on the next poll (no reprocessing / token burn);
  - a transient failure is retried up to max_attempts, then parked as needs_attention;
  - a needs_review outcome is still uploaded and counts as done (not retried);
  - the claim state machine handles stale reclaim.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.orchestration.agent_runner import ProcessOutcome
from src.orchestration.config import OrchestrationConfig
from src.orchestration.dedup import (
    DocStatus,
    InMemoryDedupStore,
    decide_claim,
    DedupRecord,
)
from src.orchestration.drive_source import LocalDirDriveClient
from src.orchestration.worker import dedup_key, run_poll


def _make_pdf(dir_path: Path, name: str, content: bytes = b"%PDF-1.4\n%%EOF\n") -> Path:
    dir_path.mkdir(parents=True, exist_ok=True)
    p = dir_path / name
    p.write_bytes(content)
    return p


def _cfg(tmp_path: Path, **overrides) -> OrchestrationConfig:
    base = dict(
        dedup_backend="memory",
        output_root=str(tmp_path / "scratch"),
        max_attempts=3,
        stale_claim_seconds=1800,
        max_files_per_poll=50,
    )
    base.update(overrides)
    return OrchestrationConfig(**base)


def _ok_process_fn(status="ok", needs_review=False):
    def _fn(agent, materialized, output_root):
        # Write a small result file so upload_results has something to copy.
        out = Path(output_root) / materialized.run_identity.safe_document_stem
        out.mkdir(parents=True, exist_ok=True)
        rp = out / "results.csv"
        rp.write_text("field,value\n", encoding="utf-8")
        return ProcessOutcome(
            status=status, needs_review=needs_review,
            result_paths={"fields_csv": str(rp)}, invoice_type_id="GENERIC",
        )
    return _fn


# --------------------------------------------------------------------------- #
# decide_claim: pure state-machine policy
# --------------------------------------------------------------------------- #

def test_decide_claim_new_and_terminal_states():
    assert decide_claim(None, now=100, max_attempts=3, stale_seconds=1800).claimable is True

    done = DedupRecord(key="k", status=DocStatus.DONE, attempts=1)
    assert decide_claim(done, 100, 3, 1800).claimable is False
    assert decide_claim(done, 100, 3, 1800).reason == "already_done"

    parked = DedupRecord(key="k", status=DocStatus.NEEDS_ATTENTION, attempts=3)
    assert decide_claim(parked, 100, 3, 1800).claimable is False


def test_decide_claim_failed_retryable_until_exhausted():
    failed = DedupRecord(key="k", status=DocStatus.FAILED, attempts=1)
    assert decide_claim(failed, 100, 3, 1800).claimable is True

    exhausted = DedupRecord(key="k", status=DocStatus.FAILED, attempts=3)
    assert decide_claim(exhausted, 100, 3, 1800).claimable is False


def test_decide_claim_stale_processing_reclaims_then_parks():
    fresh = DedupRecord(key="k", status=DocStatus.PROCESSING, attempts=1, claimed_at=100)
    assert decide_claim(fresh, now=200, max_attempts=3, stale_seconds=1800).claimable is False

    stale = DedupRecord(key="k", status=DocStatus.PROCESSING, attempts=1, claimed_at=0)
    d = decide_claim(stale, now=5000, max_attempts=3, stale_seconds=1800)
    assert d.claimable is True and d.reason == "reclaim_stale"

    stale_exhausted = DedupRecord(key="k", status=DocStatus.PROCESSING, attempts=3, claimed_at=0)
    d2 = decide_claim(stale_exhausted, now=5000, max_attempts=3, stale_seconds=1800)
    assert d2.claimable is False and d2.reason == "stale_exhausted"


# --------------------------------------------------------------------------- #
# run_poll: end-to-end with local dirs + in-memory store
# --------------------------------------------------------------------------- #

def test_clean_file_processed_once_then_skipped(tmp_path):
    in_dir, out_dir = tmp_path / "input", tmp_path / "output"
    _make_pdf(in_dir, "invoice_a.pdf")
    drive = LocalDirDriveClient(in_dir, out_dir)
    store = InMemoryDedupStore()
    cfg = _cfg(tmp_path)

    s1 = run_poll(None, drive, store, cfg, process_fn=_ok_process_fn())
    assert (s1.scanned, s1.claimed, s1.processed_ok, s1.skipped) == (1, 1, 1, 0)
    # Result landed in the output folder, not the input folder.
    assert (out_dir / "invoice_a" / "_status.txt").read_text() == "ok"

    # Second poll: same unchanged file must be skipped (no reprocessing / token burn).
    s2 = run_poll(None, drive, store, cfg, process_fn=_ok_process_fn())
    assert (s2.scanned, s2.claimed, s2.processed_ok, s2.skipped) == (1, 0, 0, 1)


def test_outputs_are_never_ingested_as_inputs(tmp_path):
    in_dir, out_dir = tmp_path / "input", tmp_path / "output"
    _make_pdf(in_dir, "invoice_a.pdf")
    drive = LocalDirDriveClient(in_dir, out_dir)
    store = InMemoryDedupStore()
    cfg = _cfg(tmp_path)

    run_poll(None, drive, store, cfg, process_fn=_ok_process_fn())
    # Even though results were written under out_dir, listing only ever sees the input dir.
    listed = {Path(r.uri).name for r in drive.list_input_documents()}
    assert listed == {"invoice_a.pdf"}


def test_transient_failure_retries_then_parks(tmp_path):
    in_dir, out_dir = tmp_path / "input", tmp_path / "output"
    _make_pdf(in_dir, "bad.pdf")
    drive = LocalDirDriveClient(in_dir, out_dir)
    store = InMemoryDedupStore()
    cfg = _cfg(tmp_path, max_attempts=3)

    def _boom(agent, materialized, output_root):
        raise RuntimeError("simulated processing failure")

    key = dedup_key(drive.list_input_documents()[0])

    # Attempts 1 and 2 -> failed (retryable).
    for _ in range(2):
        s = run_poll(None, drive, store, cfg, process_fn=_boom)
        assert s.failed == 1
        assert store.get(key).status == DocStatus.FAILED

    # Attempt 3 hits max_attempts -> parked.
    s3 = run_poll(None, drive, store, cfg, process_fn=_boom)
    assert s3.failed == 1
    assert store.get(key).status == DocStatus.NEEDS_ATTENTION

    # Subsequent polls skip the parked doc.
    s4 = run_poll(None, drive, store, cfg, process_fn=_boom)
    assert (s4.claimed, s4.skipped, s4.failed) == (0, 1, 0)


def test_needs_review_is_uploaded_and_not_retried(tmp_path):
    in_dir, out_dir = tmp_path / "input", tmp_path / "output"
    _make_pdf(in_dir, "review_me.pdf")
    drive = LocalDirDriveClient(in_dir, out_dir)
    store = InMemoryDedupStore()
    cfg = _cfg(tmp_path)

    s1 = run_poll(None, drive, store, cfg,
                  process_fn=_ok_process_fn(status="needs_review", needs_review=True))
    assert (s1.needs_review, s1.processed_ok, s1.failed) == (1, 0, 0)
    assert (out_dir / "review_me" / "_status.txt").read_text() == "needs_review"

    key = dedup_key(drive.list_input_documents()[0])
    assert store.get(key).status == DocStatus.DONE  # terminal: not reprocessed
    s2 = run_poll(None, drive, store, cfg,
                  process_fn=_ok_process_fn(status="needs_review", needs_review=True))
    assert s2.skipped == 1


def test_max_files_per_poll_caps_work(tmp_path):
    in_dir, out_dir = tmp_path / "input", tmp_path / "output"
    for i in range(5):
        _make_pdf(in_dir, f"inv_{i}.pdf")
    drive = LocalDirDriveClient(in_dir, out_dir)
    store = InMemoryDedupStore()
    cfg = _cfg(tmp_path, max_files_per_poll=2)

    s = run_poll(None, drive, store, cfg, process_fn=_ok_process_fn())
    assert s.scanned == 5 and s.claimed == 2 and s.processed_ok == 2
