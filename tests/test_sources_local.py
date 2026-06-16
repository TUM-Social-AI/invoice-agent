from pathlib import Path

import pytest

from src.sources.local import (
    SourceError,
    discover_local_documents,
    is_folder_batch,
    legacy_local_output_dir,
    materialize_local_document,
    materialize_local_input,
)


def test_discover_local_pdf_file(tmp_path: Path):
    pdf = tmp_path / "Invoice A.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")

    refs = discover_local_documents(pdf)

    assert len(refs) == 1
    assert refs[0].source_type == "local"
    assert refs[0].display_name == "Invoice A.pdf"
    assert refs[0].metadata["discovered_via"] == "file"


def test_discover_local_folder_sorts_direct_pdf_children_only(tmp_path: Path):
    (tmp_path / "b.pdf").write_bytes(b"b")
    (tmp_path / "A.pdf").write_bytes(b"a")
    (tmp_path / "notes.txt").write_text("ignore", encoding="utf-8")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "c.pdf").write_bytes(b"nested")

    refs = discover_local_documents(tmp_path)

    assert [r.display_name for r in refs] == ["A.pdf", "b.pdf"]
    assert all(r.metadata["discovered_via"] == "folder" for r in refs)


def test_discover_local_errors_for_missing_and_empty_folder(tmp_path: Path):
    with pytest.raises(SourceError, match="not found"):
        discover_local_documents(tmp_path / "missing")

    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(SourceError, match="No PDFs"):
        discover_local_documents(empty)


def test_discover_local_rejects_single_non_pdf(tmp_path: Path):
    txt = tmp_path / "invoice.txt"
    txt.write_text("not a pdf", encoding="utf-8")

    with pytest.raises(SourceError, match="Expected a PDF"):
        discover_local_documents(txt)


def test_materialize_local_document_passthrough_with_provenance(tmp_path: Path):
    pdf = tmp_path / "Example.pdf"
    pdf.write_bytes(b"%PDF-1.4\nbody")
    ref = discover_local_documents(pdf)[0]

    doc = materialize_local_document(ref)

    assert doc.local_pdf_path == str(pdf.resolve())
    assert doc.provenance.source_type == "local"
    assert doc.provenance.materialization_method == "passthrough"
    assert doc.provenance.content_sha256 is None
    assert doc.run_identity.run_id.endswith(
        f"{doc.run_identity.safe_document_stem}-{doc.run_identity.source_hash}"
    )


def test_materialize_local_document_optional_content_sha256(tmp_path: Path):
    pdf = tmp_path / "Example.pdf"
    pdf.write_bytes(b"abc")
    ref = discover_local_documents(pdf)[0]

    doc = materialize_local_document(ref, compute_content_sha256=True)

    assert doc.provenance.content_sha256 is not None
    assert len(doc.provenance.content_sha256) == 64


def test_materialize_local_input_preserves_folder_batch_marker(tmp_path: Path):
    (tmp_path / "one.pdf").write_bytes(b"1")

    docs = materialize_local_input(tmp_path)

    assert len(docs) == 1
    assert is_folder_batch(docs)


def test_legacy_local_output_dir_uses_original_local_stem(tmp_path: Path):
    pdf = tmp_path / "Invoice A.pdf"
    pdf.write_bytes(b"%PDF")
    doc = materialize_local_input(pdf)[0]

    assert legacy_local_output_dir(doc, tmp_path / "output") == tmp_path / "output" / "Invoice A"
