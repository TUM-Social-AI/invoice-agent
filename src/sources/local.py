from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from src.sources.models import DocumentRef, MaterializedDocument, SourceProvenance
from src.sources.run_identity import build_run_identity, cheap_local_source_hash, content_sha256


class SourceError(ValueError):
    """Raised when a source path cannot be discovered or materialized."""


def _modified_at_from_ns(mtime_ns: int) -> datetime:
    return datetime.fromtimestamp(mtime_ns / 1_000_000_000, tz=timezone.utc)


def _local_ref(path: Path, *, discovered_via: str) -> DocumentRef:
    resolved = path.expanduser().resolve()
    stat = resolved.stat()
    return DocumentRef(
        source_type="local",
        display_name=resolved.name,
        uri=str(resolved),
        source_id=str(resolved),
        revision_id=f"{stat.st_mtime_ns}:{stat.st_size}",
        mime_type="application/pdf",
        size_bytes=stat.st_size,
        modified_at=_modified_at_from_ns(stat.st_mtime_ns),
        metadata={
            "local_kind": "file",
            "discovered_via": discovered_via,
        },
    )


def discover_local_documents(path: str | Path) -> list[DocumentRef]:
    source_path = Path(path).expanduser()
    if not source_path.exists():
        raise SourceError(f"PDF path not found: {path}")

    if source_path.is_file():
        if source_path.suffix.lower() != ".pdf":
            raise SourceError(f"Expected a PDF file: {path}")
        return [_local_ref(source_path, discovered_via="file")]

    if not source_path.is_dir():
        raise SourceError(f"PDF path is neither a file nor a folder: {path}")

    pdfs = sorted(
        (p for p in source_path.iterdir() if p.is_file() and p.suffix.lower() == ".pdf"),
        key=lambda p: p.name.lower(),
    )
    if not pdfs:
        raise SourceError(f"No PDFs found in {path}")
    return [_local_ref(p, discovered_via="folder") for p in pdfs]


def materialize_local_document(
    ref: DocumentRef,
    *,
    compute_content_sha256: bool = False,
) -> MaterializedDocument:
    if ref.source_type != "local":
        raise SourceError(f"Cannot materialize non-local source with local materializer: {ref.source_type}")

    path = Path(ref.uri).expanduser().resolve()
    if not path.exists():
        raise SourceError(f"Local PDF not found during materialization: {ref.uri}")
    if not path.is_file() or path.suffix.lower() != ".pdf":
        raise SourceError(f"Local source is not a PDF file: {ref.uri}")

    stat = path.stat()
    source_hash = cheap_local_source_hash(path, stat=stat)
    provenance = SourceProvenance(
        source_type="local",
        source_id=ref.source_id or str(path),
        source_uri=str(path),
        display_name=ref.display_name,
        original_filename=path.name,
        revision_id=ref.revision_id or f"{stat.st_mtime_ns}:{stat.st_size}",
        source_hash=source_hash,
        content_sha256=content_sha256(path) if compute_content_sha256 else None,
        materialization_method="passthrough",
        metadata={
            **ref.metadata,
            "size_bytes": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        },
    )
    return MaterializedDocument(
        ref=ref,
        local_pdf_path=str(path),
        provenance=provenance,
        run_identity=build_run_identity(ref, provenance),
    )


def materialize_local_input(
    path: str | Path,
    *,
    compute_content_sha256: bool = False,
) -> list[MaterializedDocument]:
    refs = discover_local_documents(path)
    return [
        materialize_local_document(ref, compute_content_sha256=compute_content_sha256)
        for ref in refs
    ]


def is_folder_batch(materialized_docs: list[MaterializedDocument]) -> bool:
    return any(doc.ref.metadata.get("discovered_via") == "folder" for doc in materialized_docs)


def legacy_local_output_dir(materialized: MaterializedDocument, base_output: str | Path) -> Path:
    return Path(base_output) / Path(materialized.local_pdf_path).stem
