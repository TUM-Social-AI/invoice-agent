r"""
Per-document dedup + claim state, so a scheduled poll never double-processes a file
and a crashed task never strands one forever.

State machine per key (`sourceId#revisionId`):

    (absent) --claim--> processing --done-------> done            (terminal: skip forever)
                              |  \--fail(<max)--> failed          (retryable: reclaimed next poll)
                              |   \-fail(>=max)-> needs_attention  (terminal: parked for humans)
                              \--(claim goes stale after N s; reclaimed until attempts exhausted)

`attempts` is incremented at claim time, so a task that dies mid-document still burns
an attempt and is eventually parked instead of retried indefinitely.

The claim *decision* lives in `decide_claim` (pure) so both stores share one policy;
each store only owns its own atomic conditional write (optimistic version guard).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Optional, Protocol

logger = logging.getLogger("orchestration.dedup")


class DocStatus:
    PROCESSING = "processing"
    DONE = "done"                      # terminal, success (may still be needs_review for humans)
    FAILED = "failed"                  # transient failure, retryable on a later poll
    NEEDS_ATTENTION = "needs_attention"  # terminal, parked after exhausting retries


@dataclass
class DedupRecord:
    key: str
    status: str
    attempts: int = 0
    claimed_at: float = 0.0
    updated_at: float = 0.0
    version: int = 0
    run_id: str = ""
    needs_review: bool = False
    last_error: str = ""
    display_name: str = ""


@dataclass(frozen=True)
class ClaimDecision:
    claimable: bool
    reason: str


def decide_claim(
    record: Optional[DedupRecord],
    now: float,
    max_attempts: int,
    stale_seconds: int,
) -> ClaimDecision:
    """Pure policy: given the current record, may this poll claim the document?"""
    if record is None:
        return ClaimDecision(True, "new")

    if record.status == DocStatus.DONE:
        return ClaimDecision(False, "already_done")
    if record.status == DocStatus.NEEDS_ATTENTION:
        return ClaimDecision(False, "parked")
    if record.status == DocStatus.FAILED:
        if record.attempts >= max_attempts:
            return ClaimDecision(False, "exhausted")
        return ClaimDecision(True, "retry")
    if record.status == DocStatus.PROCESSING:
        if (now - record.claimed_at) > stale_seconds:
            if record.attempts >= max_attempts:
                # Crashed on its last allowed attempt -> should be parked, not reclaimed.
                return ClaimDecision(False, "stale_exhausted")
            return ClaimDecision(True, "reclaim_stale")
        return ClaimDecision(False, "in_progress")

    # Unknown/legacy status: treat conservatively as claimable so it makes progress.
    return ClaimDecision(True, "unknown_reset")


class DedupStore(Protocol):
    def try_claim(
        self,
        key: str,
        *,
        run_id: str,
        now: float,
        max_attempts: int,
        stale_seconds: int,
        display_name: str = "",
    ) -> ClaimDecision:
        """Atomically claim the key if policy allows. Returns the decision (claimable=True if claimed)."""
        ...

    def mark_done(self, key: str, *, run_id: str, needs_review: bool) -> None: ...

    def mark_failed(self, key: str, *, run_id: str, error: str, max_attempts: int) -> None: ...


class InMemoryDedupStore:
    """Single-process store for local runs and tests. Not durable, not shared."""

    def __init__(self) -> None:
        self._items: dict[str, DedupRecord] = {}

    def get(self, key: str) -> Optional[DedupRecord]:
        return self._items.get(key)

    def try_claim(self, key, *, run_id, now, max_attempts, stale_seconds, display_name=""):
        rec = self._items.get(key)
        decision = decide_claim(rec, now, max_attempts, stale_seconds)
        if decision.claimable:
            self._items[key] = DedupRecord(
                key=key,
                status=DocStatus.PROCESSING,
                attempts=(rec.attempts if rec else 0) + 1,
                claimed_at=now,
                updated_at=now,
                version=(rec.version + 1) if rec else 1,
                run_id=run_id,
                display_name=display_name or (rec.display_name if rec else ""),
            )
        elif decision.reason == "stale_exhausted" and rec is not None:
            self._items[key] = replace(
                rec, status=DocStatus.NEEDS_ATTENTION, updated_at=now, version=rec.version + 1
            )
        return decision

    def mark_done(self, key, *, run_id, needs_review):
        rec = self._items.get(key)
        attempts = rec.attempts if rec else 1
        version = (rec.version + 1) if rec else 1
        self._items[key] = DedupRecord(
            key=key, status=DocStatus.DONE, attempts=attempts, updated_at=0.0,
            version=version, run_id=run_id, needs_review=needs_review,
            display_name=(rec.display_name if rec else ""),
        )

    def mark_failed(self, key, *, run_id, error, max_attempts):
        rec = self._items.get(key)
        attempts = rec.attempts if rec else 1
        status = DocStatus.NEEDS_ATTENTION if attempts >= max_attempts else DocStatus.FAILED
        version = (rec.version + 1) if rec else 1
        self._items[key] = DedupRecord(
            key=key, status=status, attempts=attempts, updated_at=0.0,
            version=version, run_id=run_id, last_error=error,
            display_name=(rec.display_name if rec else ""),
        )


class DynamoDbDedupStore:
    """
    Durable, concurrency-safe store backed by a single DynamoDB table (partition key `pk`).

    Claims and terminal writes use an optimistic version guard: read the record, decide,
    then conditionally write only if the version is unchanged. A losing racer gets a
    ConditionalCheckFailedException, which we surface as a non-claim ("raced").
    """

    def __init__(self, table_name: str, region: Optional[str] = None) -> None:
        import boto3  # imported lazily so local/test paths don't require boto3

        self._table = boto3.resource("dynamodb", region_name=region).Table(table_name)

    def _get(self, key: str) -> Optional[DedupRecord]:
        item = self._table.get_item(Key={"pk": key}).get("Item")
        if not item:
            return None
        return DedupRecord(
            key=key,
            status=str(item.get("status", "")),
            attempts=int(item.get("attempts", 0)),
            claimed_at=float(item.get("claimed_at", 0)),
            updated_at=float(item.get("updated_at", 0)),
            version=int(item.get("version", 0)),
            run_id=str(item.get("run_id", "")),
            needs_review=bool(item.get("needs_review", False)),
            last_error=str(item.get("last_error", "")),
            display_name=str(item.get("display_name", "")),
        )

    def _put_guarded(self, prev: Optional[DedupRecord], rec: DedupRecord) -> None:
        from decimal import Decimal

        item = {
            "pk": rec.key,
            "status": rec.status,
            "attempts": rec.attempts,
            "claimed_at": Decimal(str(rec.claimed_at)),
            "updated_at": Decimal(str(rec.updated_at)),
            "version": rec.version,
            "run_id": rec.run_id,
            "needs_review": rec.needs_review,
            "last_error": rec.last_error[:1024],
            "display_name": rec.display_name,
        }
        if prev is None:
            self._table.put_item(Item=item, ConditionExpression="attribute_not_exists(pk)")
        else:
            self._table.put_item(
                Item=item,
                ConditionExpression="version = :v",
                ExpressionAttributeValues={":v": prev.version},
            )

    def try_claim(self, key, *, run_id, now, max_attempts, stale_seconds, display_name=""):
        from botocore.exceptions import ClientError

        prev = self._get(key)
        decision = decide_claim(prev, now, max_attempts, stale_seconds)
        try:
            if decision.claimable:
                self._put_guarded(prev, DedupRecord(
                    key=key, status=DocStatus.PROCESSING,
                    attempts=(prev.attempts if prev else 0) + 1,
                    claimed_at=now, updated_at=now,
                    version=(prev.version + 1) if prev else 1,
                    run_id=run_id,
                    display_name=display_name or (prev.display_name if prev else ""),
                ))
            elif decision.reason == "stale_exhausted" and prev is not None:
                self._put_guarded(prev, replace(
                    prev, status=DocStatus.NEEDS_ATTENTION, updated_at=now, version=prev.version + 1
                ))
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return ClaimDecision(False, "raced")
            raise
        return decision

    def _terminal_write(self, key: str, run_id: str, status: str, *, needs_review=False, error="") -> None:
        from botocore.exceptions import ClientError

        # We hold the claim, so a single guarded write should succeed; retry once on a
        # (rare) version race before giving up to a last-writer-wins terminal write.
        for guard in (True, False):
            prev = self._get(key)
            rec = DedupRecord(
                key=key, status=status, attempts=(prev.attempts if prev else 1), updated_at=0.0,
                version=((prev.version + 1) if prev else 1),
                run_id=run_id, needs_review=needs_review, last_error=error,
                display_name=(prev.display_name if prev else ""),
            )
            try:
                if guard:
                    self._put_guarded(prev, rec)
                else:
                    self._table.put_item(Item=self._to_item(rec))  # unconditional fallback
                return
            except ClientError as e:
                if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                    logger.warning("terminal write race on %s; retrying unconditionally", key)
                    continue
                raise

    def _to_item(self, rec: DedupRecord) -> dict:
        from decimal import Decimal

        return {
            "pk": rec.key, "status": rec.status, "attempts": rec.attempts,
            "claimed_at": Decimal(str(rec.claimed_at)), "updated_at": Decimal(str(rec.updated_at)),
            "version": rec.version, "run_id": rec.run_id, "needs_review": rec.needs_review,
            "last_error": rec.last_error[:1024], "display_name": rec.display_name,
        }

    def mark_done(self, key, *, run_id, needs_review):
        self._terminal_write(key, run_id, DocStatus.DONE, needs_review=needs_review)

    def mark_failed(self, key, *, run_id, error, max_attempts):
        prev = self._get(key)
        attempts = prev.attempts if prev else 1
        status = DocStatus.NEEDS_ATTENTION if attempts >= max_attempts else DocStatus.FAILED
        self._terminal_write(key, run_id, status, error=error)
