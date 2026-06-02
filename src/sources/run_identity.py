from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from os import stat_result
from pathlib import Path

from src.sources.models import DocumentRef, RunIdentity, SourceProvenance

def safe_document_stem(display_name: str, *, max_len: int = 64) -> str:
    stem = Path(display_name).stem or str(display_name)
    safe = re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")
    if not safe:
        safe = "document"
    return safe[:max_len].strip("-") or "document"


def cheap_local_source_hash(path: Path, *, stat: stat_result | None = None) -> str:
    resolved = path.expanduser().resolve()
    st = stat if stat is not None else (resolved.stat() if resolved.exists() else None)
    if st:
        identity = f"local:{resolved}:{st.st_mtime_ns}:{st.st_size}"
    else:
        identity = f"local:{resolved}:missing"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]


def content_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def build_run_identity(
    ref: DocumentRef,
    provenance: SourceProvenance,
    *,
    now: datetime | None = None,
) -> RunIdentity:
    created = now or datetime.now(timezone.utc)
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    created = created.astimezone(timezone.utc)
    safe_stem = safe_document_stem(ref.display_name)
    timestamp = created.strftime("%Y%m%dT%H%M%S%fZ")
    run_id = f"{timestamp}-{safe_stem}-{provenance.source_hash}"
    return RunIdentity(
        run_id=run_id,
        created_at_utc=created,
        safe_document_stem=safe_stem,
        source_hash=provenance.source_hash,
    )
