from datetime import datetime, timezone
from pathlib import Path

from src.sources.local import discover_local_documents, materialize_local_document
from src.sources.run_identity import build_run_identity, safe_document_stem


def test_safe_document_stem_sanitizes_and_falls_back():
    assert safe_document_stem("Invoice A.5.pdf") == "invoice-a-5"
    assert safe_document_stem("!!!.pdf") == "document"


def test_build_run_identity_uses_utc_timestamp_stem_and_hash(tmp_path: Path):
    pdf = tmp_path / "Invoice A.pdf"
    pdf.write_bytes(b"x")
    doc = materialize_local_document(discover_local_documents(pdf)[0])
    now = datetime(2026, 5, 31, 12, 45, 1, tzinfo=timezone.utc)

    run_identity = build_run_identity(doc.ref, doc.provenance, now=now)

    assert run_identity.run_id == f"20260531T124501000000Z-invoice-a-{doc.provenance.source_hash}"
    assert run_identity.safe_document_stem == "invoice-a"


def test_local_source_hash_distinguishes_safe_stem_collisions(tmp_path: Path):
    a = tmp_path / "Invoice A.pdf"
    b = tmp_path / "invoice_a.pdf"
    a.write_bytes(b"a")
    b.write_bytes(b"b")

    doc_a = materialize_local_document(discover_local_documents(a)[0])
    doc_b = materialize_local_document(discover_local_documents(b)[0])

    assert doc_a.run_identity.safe_document_stem == doc_b.run_identity.safe_document_stem
    assert doc_a.run_identity.source_hash != doc_b.run_identity.source_hash
