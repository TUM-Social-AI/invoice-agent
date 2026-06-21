from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SourceType = Literal["local", "google_drive"]
MaterializationMethod = Literal["passthrough", "download", "export"]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class DocumentRef(BaseModel):
    """Stable reference to a source document before agent processing."""

    model_config = ConfigDict(extra="forbid")

    source_type: SourceType
    display_name: str
    uri: str
    source_id: str | None = None
    revision_id: str | None = None
    mime_type: str | None = None
    size_bytes: int | None = None
    modified_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SourceProvenance(BaseModel):
    """Traceable origin details for a materialized document."""

    model_config = ConfigDict(extra="forbid")

    source_type: SourceType
    source_id: str
    source_uri: str
    display_name: str
    original_filename: str
    revision_id: str | None = None
    source_hash: str
    content_sha256: str | None = None
    discovered_at_utc: datetime = Field(default_factory=utc_now)
    materialized_at_utc: datetime = Field(default_factory=utc_now)
    materialization_method: MaterializationMethod
    metadata: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_local_path_minimal(cls, pdf_path: str) -> "SourceProvenance":
        path = Path(pdf_path)
        resolved = path.expanduser().resolve()
        stat = resolved.stat() if resolved.exists() else None
        size = stat.st_size if stat else None
        mtime_ns = stat.st_mtime_ns if stat else None
        source_id = str(resolved)
        revision_id = f"{mtime_ns}:{size}" if stat else None
        from src.sources.run_identity import cheap_local_source_hash

        return cls(
            source_type="local",
            source_id=source_id,
            source_uri=str(resolved),
            display_name=resolved.name,
            original_filename=resolved.name,
            revision_id=revision_id,
            source_hash=cheap_local_source_hash(resolved, stat=stat),
            materialization_method="passthrough",
            metadata={
                "local_kind": "file",
                "size_bytes": size,
                "mtime_ns": mtime_ns,
                "provenance_quality": "minimal",
            },
        )


class RunIdentity(BaseModel):
    """Identifier used to connect logs, outputs, and future upload folders."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    created_at_utc: datetime
    safe_document_stem: str
    source_hash: str


class MaterializedDocument(BaseModel):
    """A source document materialized to the local PDF path expected by the agent."""

    model_config = ConfigDict(extra="forbid")

    ref: DocumentRef
    local_pdf_path: str
    provenance: SourceProvenance
    run_identity: RunIdentity
