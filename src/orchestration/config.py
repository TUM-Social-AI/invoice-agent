"""Orchestration settings, read from the app config (`orchestration:` block).

Drive input/output folders are NOT here — they live under `sources.google_drive`
(google-drive-source branch) and are resolved by the Drive adapter, not this config.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class OrchestrationConfig:
    # Per-poll safety + retry behavior.
    max_files_per_poll: int = 50          # bound token spend / runtime per scheduled run
    max_attempts: int = 3                 # retryable failures before a doc is parked for humans
    stale_claim_seconds: int = 1800       # a "processing" claim older than this is reclaimable (crashed task)

    # Dedup backend: "dynamodb" (prod) or "memory" (local/test).
    dedup_backend: str = "dynamodb"
    dynamodb_table: str = "invoice-agent-dedup"
    aws_region: Optional[str] = None      # None -> boto3 default (env / task role region)

    # Local scratch dir for per-document result files before they are uploaded.
    output_root: str = "output"

    @classmethod
    def from_app_config(cls, config: dict[str, Any]) -> "OrchestrationConfig":
        orch = config.get("orchestration") or {}
        region = orch.get("aws_region")
        return cls(
            max_files_per_poll=int(orch.get("max_files_per_poll", cls.max_files_per_poll)),
            max_attempts=int(orch.get("max_attempts", cls.max_attempts)),
            stale_claim_seconds=int(orch.get("stale_claim_seconds", cls.stale_claim_seconds)),
            dedup_backend=str(orch.get("dedup_backend", cls.dedup_backend)).strip().lower(),
            dynamodb_table=str(orch.get("dynamodb_table", cls.dynamodb_table)),
            aws_region=(str(region) if region else None),
            output_root=str(orch.get("output_root", cls.output_root)),
        )
