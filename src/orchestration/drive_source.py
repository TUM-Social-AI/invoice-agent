"""
The seam between the orchestration loop and the document source.

`DriveClient` is the interface the (branch-owned) Google Drive client must implement.
`LocalDirDriveClient` is a filesystem-backed implementation that treats two local
directories as the input/output folders, so the whole loop runs end-to-end with no
AWS and no Drive credentials — used for local runs and tests.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Protocol, runtime_checkable

from src.sources.local import discover_local_documents, materialize_local_document
from src.sources.models import DocumentRef, MaterializedDocument


@runtime_checkable
class DriveClient(Protocol):
    """
    Contract for a document source.

    Implementations MUST:
      - scope `list_input_documents` to the configured *input* folder only, and never
        surface files from the output folder (otherwise the poller re-ingests its own
        results and loops, burning tokens every cycle);
      - return only PDF documents;
      - set `source_id` to the stable source identifier (Drive fileId) and `revision_id`
        to the source revision (Drive revisionId / headRevisionId), so that replacing a
        file yields a new dedup key and is reprocessed, while an unchanged file is skipped.
    """

    def list_input_documents(self) -> list[DocumentRef]:
        """Return refs for new/candidate PDFs in the input folder (output folder excluded)."""
        ...

    def materialize(self, ref: DocumentRef) -> MaterializedDocument:
        """Download/resolve the document to a local PDF path the agent can read."""
        ...

    def upload_results(self, ref: DocumentRef, result_paths: dict[str, str], *, status: str) -> None:
        """Publish the agent's output files back to the output folder for this document."""
        ...

    def cleanup(self, materialized: MaterializedDocument) -> None:
        """Release any local scratch created by `materialize` (no-op for passthrough sources)."""
        ...


class LocalDirDriveClient:
    """
    Filesystem stand-in for the Drive client.

    `input_dir` plays the role of the Drive input folder and `output_dir` the output
    folder. Because they are distinct directories, results written by `upload_results`
    are structurally excluded from `list_input_documents` — the same guarantee the real
    Drive client must provide by scoping to distinct folder IDs.
    """

    def __init__(self, input_dir: str | Path, output_dir: str | Path) -> None:
        self.input_dir = Path(input_dir)
        self.output_dir = Path(output_dir)

    def list_input_documents(self) -> list[DocumentRef]:
        if not self.input_dir.exists():
            return []
        try:
            # discover_local_documents already filters to *.pdf and sorts deterministically.
            return discover_local_documents(self.input_dir)
        except Exception:
            # No PDFs present (SourceError) -> nothing to do this poll.
            return []

    def materialize(self, ref: DocumentRef) -> MaterializedDocument:
        return materialize_local_document(ref)

    def upload_results(self, ref: DocumentRef, result_paths: dict[str, str], *, status: str) -> None:
        dest = self.output_dir / Path(ref.uri).stem
        dest.mkdir(parents=True, exist_ok=True)
        for src_path in result_paths.values():
            src = Path(src_path)
            if src.exists():
                shutil.copy2(src, dest / src.name)
        # A simple status marker; the real Drive client may instead set file appProperties.
        (dest / "_status.txt").write_text(status, encoding="utf-8")

    def cleanup(self, materialized: MaterializedDocument) -> None:
        # Passthrough source — the "local PDF" is the original file, nothing to remove.
        return None
