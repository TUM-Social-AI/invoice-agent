"""
DynamoDB-backed dedup store tests using moto (mocked AWS).

Covers the concurrency-critical behavior the in-memory store cannot: conditional-write
claims against a real table shape, the optimistic version guard, stale-claim reclaim,
and — most importantly — the raced-claim rejection that stops two workers from
double-processing the same document.
"""

from __future__ import annotations

import pytest

pytest.importorskip("moto")

import boto3  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402
from moto import mock_aws  # noqa: E402

from src.orchestration.dedup import DedupRecord, DocStatus, DynamoDbDedupStore  # noqa: E402

TABLE = "invoice-agent-dedup-test"
REGION = "us-east-1"


@pytest.fixture
def store(monkeypatch):
    # moto still needs credentials/region to resolve, even though it never calls AWS.
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    with mock_aws():
        boto3.resource("dynamodb", region_name=REGION).create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield DynamoDbDedupStore(TABLE, region=REGION)


def _claim(store, key, *, run_id="w", now=1000.0, max_attempts=3, stale=1800):
    return store.try_claim(
        key, run_id=run_id, now=now, max_attempts=max_attempts, stale_seconds=stale
    )


def test_claim_creates_processing_and_blocks_fresh_second_claim(store):
    d1 = _claim(store, "f#1", run_id="A")
    assert d1.claimable is True
    rec = store._get("f#1")
    assert rec.status == DocStatus.PROCESSING and rec.attempts == 1

    # A second, different worker sees the fresh claim and must not take it.
    d2 = _claim(store, "f#1", run_id="B", now=1001.0)
    assert d2.claimable is False and d2.reason == "in_progress"


def test_mark_done_is_terminal(store):
    _claim(store, "f#1")
    store.mark_done("f#1", run_id="w", needs_review=True)
    rec = store._get("f#1")
    assert rec.status == DocStatus.DONE and rec.needs_review is True
    # Terminal: never claimable again, even far in the future.
    assert _claim(store, "f#1", now=99_999.0).claimable is False


def test_retry_until_parked(store):
    key = "f#1"
    for _ in range(2):
        assert _claim(store, key).claimable is True
        store.mark_failed(key, run_id="w", error="boom", max_attempts=3)
        assert store._get(key).status == DocStatus.FAILED

    # Third attempt reaches the cap -> parked.
    assert _claim(store, key).claimable is True
    store.mark_failed(key, run_id="w", error="boom", max_attempts=3)
    assert store._get(key).status == DocStatus.NEEDS_ATTENTION
    assert store._get(key).attempts == 3

    # Parked docs are never reclaimed.
    assert _claim(store, key, now=99_999.0).claimable is False


def test_stale_processing_is_reclaimed(store):
    key = "f#1"
    assert _claim(store, key, now=0.0).claimable is True  # attempts=1, claimed_at=0
    d = _claim(store, key, now=5000.0)  # 5000 > stale(1800) -> reclaim
    assert d.claimable is True and d.reason == "reclaim_stale"
    assert store._get(key).attempts == 2


def test_stale_but_exhausted_is_parked(store):
    key = "f#1"
    _claim(store, key, now=0.0)      # attempts 1
    _claim(store, key, now=5000.0)   # stale reclaim -> attempts 2
    _claim(store, key, now=10_000.0)  # stale reclaim -> attempts 3
    d = _claim(store, key, now=20_000.0)  # stale AND attempts>=max -> park, don't reclaim
    assert d.claimable is False and d.reason == "stale_exhausted"
    assert store._get(key).status == DocStatus.NEEDS_ATTENTION


def test_raced_claim_is_rejected(store, monkeypatch):
    key = "f#1"
    assert _claim(store, key, run_id="A").claimable is True  # A holds the claim

    # Simulate B reading stale (row appears absent). The conditional write must still
    # fail because the row now exists -> B is told it raced, not allowed to proceed.
    monkeypatch.setattr(store, "_get", lambda k: None)
    d = store.try_claim(key, run_id="B", now=1001.0, max_attempts=3, stale_seconds=1800)
    assert d.claimable is False and d.reason == "raced"


def test_put_guarded_rejects_stale_version(store):
    key = "f#1"
    _claim(store, key)                        # version 1
    stale_prev = store._get(key)              # captured at version 1
    store.mark_done(key, run_id="w", needs_review=False)  # advances to version 2

    # A write guarded by the stale version-1 must be rejected by DynamoDB.
    with pytest.raises(ClientError):
        store._put_guarded(
            stale_prev,
            DedupRecord(key=key, status=DocStatus.PROCESSING, version=stale_prev.version + 1),
        )
