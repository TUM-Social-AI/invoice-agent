from __future__ import annotations

import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

from src.sources import google_drive as gd
from src.sources.models import DocumentRef


def _app_config(tmp_path: Path) -> dict:
    return {
        "sources": {
            "google_drive": {
                "scopes": ["https://www.googleapis.com/auth/drive"],
                "oauth_client_secret_path": str(tmp_path / "client.json"),
                "token_path": str(tmp_path / "token.json"),
                "materialized_root": str(tmp_path / "materialized"),
                "include_shared_drives": True,
                "page_size": 2,
                "cleanup_downloads": True,
            }
        }
    }


class FakeExecuteRequest:
    def __init__(self, response):
        self.response = response

    def execute(self):
        return self.response


class FakeFiles:
    def __init__(self, pages=None):
        self.pages = list(pages or [])
        self.list_calls = []
        self.media_calls = []

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        return FakeExecuteRequest(self.pages.pop(0))

    def get_media(self, **kwargs):
        self.media_calls.append(kwargs)
        return object()


class FakeService:
    def __init__(self, pages=None):
        self._files = FakeFiles(pages)

    def files(self):
        return self._files


def test_discover_google_drive_documents_lists_pdfs_with_shared_drive_flags(tmp_path: Path):
    service = FakeService([
        {
            "nextPageToken": "next",
            "files": [
                {
                    "id": "file-b",
                    "name": "B.pdf",
                    "mimeType": "application/pdf",
                    "size": "12",
                    "modifiedTime": "2026-06-01T12:00:00.000Z",
                    "md5Checksum": "md5-b",
                    "headRevisionId": "rev-b",
                    "webViewLink": "https://drive/b",
                }
            ],
        },
        {
            "files": [
                {
                    "id": "file-a",
                    "name": "a.pdf",
                    "mimeType": "application/pdf",
                    "size": "7",
                    "modifiedTime": "2026-06-02T12:00:00.000Z",
                    "md5Checksum": "md5-a",
                    "webViewLink": "https://drive/a",
                }
            ],
        },
    ])

    refs = gd.discover_google_drive_documents("folder-1", _app_config(tmp_path), service=service)

    assert [r.display_name for r in refs] == ["a.pdf", "B.pdf"]
    assert refs[0].source_type == "google_drive"
    assert refs[0].source_id == "file-a"
    assert refs[0].uri == "gdrive://file-a"
    assert refs[0].revision_id == "md5-a"
    assert refs[0].size_bytes == 7
    assert refs[0].metadata["drive_folder_id"] == "folder-1"
    assert refs[0].metadata["discovered_via"] == "google_drive_folder"

    first_call = service.files().list_calls[0]
    assert first_call["supportsAllDrives"] is True
    assert first_call["includeItemsFromAllDrives"] is True
    assert first_call["pageSize"] == 2
    assert "mimeType = 'application/pdf'" in first_call["q"]
    assert service.files().list_calls[1]["pageToken"] == "next"


def test_discover_google_drive_documents_handles_empty_folder(tmp_path: Path):
    service = FakeService([{"files": []}])

    refs = gd.discover_google_drive_documents("folder-1", _app_config(tmp_path), service=service)

    assert refs == []


def test_extract_google_drive_folder_id_accepts_raw_id_and_folder_url():
    assert gd.extract_google_drive_folder_id("folder-1") == "folder-1"
    assert (
        gd.extract_google_drive_folder_id("https://drive.google.com/drive/folders/folder-1?usp=sharing")
        == "folder-1"
    )


def test_resolve_google_drive_folder_id_uses_override_then_config_url(tmp_path: Path):
    config = _app_config(tmp_path)
    config["sources"]["google_drive"]["folder_url"] = "https://drive.google.com/drive/folders/from-config"

    assert gd.resolve_google_drive_folder_id(config, override="from-cli") == "from-cli"
    assert gd.resolve_google_drive_folder_id(config) == "from-config"


def test_resolve_google_drive_config_folder_id_uses_config_folder_url(tmp_path: Path):
    config = _app_config(tmp_path)
    config["sources"]["google_drive"]["config_folder"] = {
        "enabled": True,
        "folder_url": "https://drive.google.com/drive/folders/config-folder",
    }

    assert gd.google_drive_config_folder_enabled(config) is True
    assert gd.resolve_google_drive_config_folder_id(config) == "config-folder"


def test_discover_google_drive_config_files_lists_folder_contents(tmp_path: Path):
    service = FakeService([
        {
            "files": [
                {
                    "id": "rules",
                    "name": "compliance_rules.csv",
                    "mimeType": "text/csv",
                    "modifiedTime": "2026-06-01T12:00:00.000Z",
                },
                {
                    "id": "notes",
                    "name": "notes.md",
                    "mimeType": "text/markdown",
                    "modifiedTime": "2026-06-01T12:00:00.000Z",
                },
            ],
        },
    ])

    refs = gd.discover_google_drive_config_files("folder-1", _app_config(tmp_path), service=service)

    assert [r.display_name for r in refs] == ["compliance_rules.csv", "notes.md"]
    first_call = service.files().list_calls[0]
    assert first_call["supportsAllDrives"] is True
    assert first_call["includeItemsFromAllDrives"] is True
    assert "mimeType = 'application/pdf'" not in first_call["q"]


def test_materialize_google_drive_config_folder_requires_core_csvs(tmp_path: Path):
    config = _app_config(tmp_path)
    config["sources"]["google_drive"]["config_folder"] = {"folder_id": "folder-1"}
    service = FakeService([
        {
            "files": [
                {"id": "types", "name": "invoice_types.csv", "mimeType": "text/csv"},
                {"id": "fields", "name": "extraction_fields.csv", "mimeType": "text/csv"},
            ],
        },
    ])

    with pytest.raises(gd.GoogleDriveSourceError, match="compliance_rules.csv"):
        gd.materialize_google_drive_config_folder(config, service=service)


def test_materialize_google_drive_config_folder_rejects_duplicate_config_names(tmp_path: Path):
    config = _app_config(tmp_path)
    config["sources"]["google_drive"]["config_folder"] = {"folder_id": "folder-1"}
    service = FakeService([
        {
            "files": [
                {"id": "types-a", "name": "invoice_types.csv", "mimeType": "text/csv"},
                {"id": "types-b", "name": "invoice_types.csv", "mimeType": "text/csv"},
                {"id": "fields", "name": "extraction_fields.csv", "mimeType": "text/csv"},
                {"id": "rules", "name": "compliance_rules.csv", "mimeType": "text/csv"},
            ],
        },
    ])

    with pytest.raises(gd.GoogleDriveSourceError, match="duplicate"):
        gd.materialize_google_drive_config_folder(config, service=service)


def test_materialize_google_drive_config_folder_downloads_loadable_config(
    tmp_path: Path,
    monkeypatch,
):
    from src.config.loader import load_config

    config = _app_config(tmp_path)
    config["sources"]["google_drive"]["config_folder"] = {
        "folder_id": "folder-1",
        "materialized_root": str(tmp_path / "drive-config"),
    }
    service = FakeService([
        {
            "files": [
                {"id": "types", "name": "invoice_types.csv", "mimeType": "text/csv", "headRevisionId": "1"},
                {"id": "fields", "name": "extraction_fields.csv", "mimeType": "text/csv", "headRevisionId": "1"},
                {"id": "rules", "name": "compliance_rules.csv", "mimeType": "text/csv", "headRevisionId": "1"},
                {"id": "allowed", "name": "allowed_values.csv", "mimeType": "text/csv", "headRevisionId": "1"},
                {"id": "deny", "name": "employee_name_role_denylist.txt", "mimeType": "text/plain", "headRevisionId": "1"},
            ],
        },
    ])
    payloads = {
        "types": (
            "invoice_type_id,display_name,description,agent_context,enabled\n"
            "TEST,Test Invoice,Test description,Test context,true\n"
        ),
        "fields": (
            "field_id,invoice_type_id,field_name,field_label,data_type,required,extraction_hint,page_region,aliases\n"
            "F_TEST,TEST,vendor_name,Vendor,string,true,Find vendor,header,Supplier\n"
        ),
        "rules": (
            "rule_id,invoice_type_id,rule_name,field_id,check_type,check_value,severity,agent_hint,error_message,page_region,enabled,rule_group\n"
            "R_TEST,TEST,vendor_required,F_TEST,required,,error,Vendor is required,Missing vendor,header,true,general\n"
        ),
        "allowed": "field_name,invoice_type_id,value\nvendor_name,TEST,ACME\n",
        "deny": "manager\n",
    }

    def fake_download(_request, path):
        file_id = service.files().media_calls[-1]["fileId"]
        path.write_text(payloads[file_id], encoding="utf-8")

    monkeypatch.setattr(gd, "_download_media_to_path", fake_download)

    config_dir = gd.materialize_google_drive_config_folder(config, service=service)
    store = load_config(str(config_dir))

    assert sorted(p.name for p in config_dir.iterdir()) == [
        "allowed_values.csv",
        "compliance_rules.csv",
        "employee_name_role_denylist.txt",
        "extraction_fields.csv",
        "invoice_types.csv",
    ]
    assert "TEST" in store.invoice_types
    assert store.get_fields("TEST")[0].allowed_values == ["ACME"]
    assert store.get_rules("TEST")[0].rule_id == "R_TEST"
    assert store.employee_name_role_denylist == ["manager"]


def test_materialize_google_drive_document_downloads_and_builds_provenance(
    tmp_path: Path,
    monkeypatch,
):
    service = FakeService()
    ref = DocumentRef(
        source_type="google_drive",
        display_name="Invoice A.pdf",
        uri="gdrive://file-1",
        source_id="file-1",
        revision_id="rev-1",
        mime_type="application/pdf",
        size_bytes=3,
        metadata={
            "drive_folder_id": "folder-1",
            "md5_checksum": "md5",
            "modified_time": "2026-06-01T12:00:00.000Z",
        },
    )

    def fake_download(_request, path):
        path.write_bytes(b"%PDF")

    monkeypatch.setattr(gd, "_download_media_to_path", fake_download)

    doc = gd.materialize_google_drive_document(ref, _app_config(tmp_path), service=service)

    local_path = Path(doc.local_pdf_path)
    assert local_path.exists()
    assert local_path.name == "invoice-a.pdf"
    assert doc.provenance.source_type == "google_drive"
    assert doc.provenance.materialization_method == "download"
    assert doc.provenance.content_sha256 is not None
    assert doc.provenance.metadata["materialized_path"] == str(local_path)
    assert doc.run_identity.safe_document_stem == "invoice-a"
    assert service.files().media_calls == [{"fileId": "file-1", "supportsAllDrives": True}]


def test_cleanup_materialized_google_drive_document_deletes_only_drive_download(tmp_path: Path, monkeypatch):
    service = FakeService()
    ref = DocumentRef(
        source_type="google_drive",
        display_name="Invoice A.pdf",
        uri="gdrive://file-1",
        source_id="file-1",
        revision_id="rev-1",
        mime_type="application/pdf",
    )
    monkeypatch.setattr(gd, "_download_media_to_path", lambda _request, path: path.write_bytes(b"%PDF"))
    doc = gd.materialize_google_drive_document(ref, _app_config(tmp_path), service=service)
    parent = Path(doc.local_pdf_path).parent

    gd.cleanup_materialized_google_drive_document(doc)

    assert not Path(doc.local_pdf_path).exists()
    assert not parent.exists()


def test_cleanup_ignores_local_document(tmp_path: Path):
    local_pdf = tmp_path / "local.pdf"
    local_pdf.write_bytes(b"%PDF")
    ref = DocumentRef(
        source_type="local",
        display_name="local.pdf",
        uri=str(local_pdf),
        source_id=str(local_pdf),
        mime_type="application/pdf",
    )
    from src.sources.local import materialize_local_document

    doc = materialize_local_document(ref)

    gd.cleanup_materialized_google_drive_document(doc)

    assert local_pdf.exists()


def test_google_drive_output_dir_uses_safe_stem_and_source_hash(tmp_path: Path, monkeypatch):
    service = FakeService()
    ref = DocumentRef(
        source_type="google_drive",
        display_name="Invoice A.pdf",
        uri="gdrive://file-1",
        source_id="file-1",
        revision_id="rev-1",
        mime_type="application/pdf",
    )
    monkeypatch.setattr(gd, "_download_media_to_path", lambda _request, path: path.write_bytes(b"%PDF"))
    doc = gd.materialize_google_drive_document(ref, _app_config(tmp_path), service=service)

    out = gd.google_drive_output_dir(doc, tmp_path / "output")

    assert out.name == f"invoice-a-{doc.run_identity.source_hash}"


def _install_fake_google_oauth_modules(monkeypatch, fake_credentials_cls, fake_flow_cls, fake_request_cls=None):
    google_mod = ModuleType("google")
    auth_mod = ModuleType("google.auth")
    transport_mod = ModuleType("google.auth.transport")
    requests_mod = ModuleType("google.auth.transport.requests")
    oauth2_mod = ModuleType("google.oauth2")
    credentials_mod = ModuleType("google.oauth2.credentials")
    flow_mod = ModuleType("google_auth_oauthlib.flow")
    oauthlib_mod = ModuleType("google_auth_oauthlib")

    requests_mod.Request = fake_request_cls or type("FakeRequest", (), {})
    credentials_mod.Credentials = fake_credentials_cls
    flow_mod.InstalledAppFlow = fake_flow_cls

    monkeypatch.setitem(sys.modules, "google", google_mod)
    monkeypatch.setitem(sys.modules, "google.auth", auth_mod)
    monkeypatch.setitem(sys.modules, "google.auth.transport", transport_mod)
    monkeypatch.setitem(sys.modules, "google.auth.transport.requests", requests_mod)
    monkeypatch.setitem(sys.modules, "google.oauth2", oauth2_mod)
    monkeypatch.setitem(sys.modules, "google.oauth2.credentials", credentials_mod)
    monkeypatch.setitem(sys.modules, "google_auth_oauthlib", oauthlib_mod)
    monkeypatch.setitem(sys.modules, "google_auth_oauthlib.flow", flow_mod)


def test_resolve_google_drive_credentials_loads_valid_token(tmp_path: Path, monkeypatch):
    config = _app_config(tmp_path)
    token = tmp_path / "token.json"
    token.write_text("{}", encoding="utf-8")

    class FakeCredentials:
        valid = True
        expired = False
        refresh_token = "refresh"

        @classmethod
        def from_authorized_user_file(cls, path, scopes):
            assert path == str(token)
            assert scopes == ["https://www.googleapis.com/auth/drive"]
            return cls()

    class FakeFlow:
        pass

    _install_fake_google_oauth_modules(monkeypatch, FakeCredentials, FakeFlow)

    creds = gd.resolve_google_drive_credentials(config)

    assert isinstance(creds, FakeCredentials)


def test_resolve_google_drive_credentials_runs_flow_and_saves_token(tmp_path: Path, monkeypatch):
    config = _app_config(tmp_path)
    client = tmp_path / "client.json"
    client.write_text(
        json.dumps({
            "installed": {
                "client_id": "id",
                "client_secret": "secret",
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        }),
        encoding="utf-8",
    )

    class FakeCredentials:
        valid = True
        expired = False
        refresh_token = "refresh"

        @classmethod
        def from_authorized_user_file(cls, path, scopes):
            raise AssertionError("token should not be loaded")

        def to_json(self):
            return '{"token": "saved"}'

    class FakeFlow:
        @classmethod
        def from_client_secrets_file(cls, path, scopes):
            assert path == str(client)
            assert scopes == ["https://www.googleapis.com/auth/drive"]
            return cls()

        def run_local_server(self, port):
            assert port == 0
            return FakeCredentials()

    _install_fake_google_oauth_modules(monkeypatch, FakeCredentials, FakeFlow)

    creds = gd.resolve_google_drive_credentials(config)

    assert isinstance(creds, FakeCredentials)
    assert (tmp_path / "token.json").read_text(encoding="utf-8") == '{"token": "saved"}'


def test_resolve_google_drive_credentials_errors_for_missing_client_json(tmp_path: Path, monkeypatch):
    config = _app_config(tmp_path)

    class FakeCredentials:
        pass

    class FakeFlow:
        pass

    _install_fake_google_oauth_modules(monkeypatch, FakeCredentials, FakeFlow)

    with pytest.raises(gd.GoogleDriveSourceError, match="OAuth client JSON not found"):
        gd.resolve_google_drive_credentials(config)
