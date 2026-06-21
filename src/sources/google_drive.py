from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from src.sources.models import DocumentRef, MaterializedDocument, SourceProvenance
from src.sources.run_identity import build_run_identity, content_sha256, safe_document_stem

logger = logging.getLogger(__name__)

DRIVE_PDF_MIME_TYPE = "application/pdf"
DEFAULT_DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
_DRIVE_FOLDER_PATH_RE = re.compile(r"/folders/([^/?#]+)")
REQUIRED_CONFIG_FILES = {
    "invoice_types.csv",
    "extraction_fields.csv",
    "compliance_rules.csv",
}
OPTIONAL_CONFIG_FILES = {
    "allowed_values.csv",
    "employee_name_role_denylist.txt",
}
ALL_CONFIG_FILES = REQUIRED_CONFIG_FILES | OPTIONAL_CONFIG_FILES


class GoogleDriveSourceError(ValueError):
    """Raised when Google Drive discovery, auth, or materialization fails."""


def _drive_config(app_config: dict) -> dict:
    return ((app_config or {}).get("sources") or {}).get("google_drive") or {}


def _drive_config_folder_config(app_config: dict) -> dict:
    return (_drive_config(app_config).get("config_folder") or {})


def resolve_google_drive_folder_id(app_config: dict, *, override: str | None = None) -> str:
    """Resolve a Drive folder ID from CLI override, config ID, or config URL."""

    cfg = _drive_config(app_config)
    raw = override or cfg.get("folder_id") or cfg.get("folder_url") or ""
    raw = str(raw).strip()
    if not raw:
        raise GoogleDriveSourceError(
            "Google Drive folder is not configured. Pass --google-drive-folder-id or set "
            "sources.google_drive.folder_id / folder_url in config/config.yaml."
        )
    return extract_google_drive_folder_id(raw)


def google_drive_config_folder_enabled(app_config: dict) -> bool:
    cfg = _drive_config_folder_config(app_config)
    return bool(cfg.get("enabled", False))


def resolve_google_drive_config_folder_id(app_config: dict) -> str:
    """Resolve the Drive folder ID containing CSV config files."""

    cfg = _drive_config_folder_config(app_config)
    raw = cfg.get("folder_id") or cfg.get("folder_url") or ""
    raw = str(raw).strip()
    if not raw:
        raise GoogleDriveSourceError(
            "Google Drive config folder is enabled but not configured. Set "
            "sources.google_drive.config_folder.folder_id or folder_url in config/config.yaml."
        )
    return extract_google_drive_folder_id(raw)


def extract_google_drive_folder_id(value: str) -> str:
    """Accept either a raw folder ID or a standard Google Drive folder URL."""

    value = str(value).strip()
    if not value:
        raise GoogleDriveSourceError("Google Drive folder ID or URL is empty.")
    if "://" not in value:
        return value

    parsed = urlparse(value)
    match = _DRIVE_FOLDER_PATH_RE.search(parsed.path)
    if match:
        return match.group(1)

    query = parse_qs(parsed.query)
    for key in ("id", "folderId"):
        ids = query.get(key) or []
        if ids and ids[0].strip():
            return ids[0].strip()

    raise GoogleDriveSourceError(f"Could not extract Google Drive folder ID from URL: {value}")


def _drive_scopes(cfg: dict) -> list[str]:
    scopes = cfg.get("scopes") or [DEFAULT_DRIVE_SCOPE]
    if isinstance(scopes, str):
        scopes = [scopes]
    cleaned = [str(s).strip() for s in scopes if str(s).strip()]
    return cleaned or [DEFAULT_DRIVE_SCOPE]


def _resolve_oauth_client_secret_path(cfg: dict, override: str | None = None) -> Path:
    raw = override
    if not raw:
        env_name = str(cfg.get("oauth_client_secret_path_env", "GOOGLE_DRIVE_OAUTH_CLIENT_SECRET")).strip()
        raw = os.getenv(env_name) if env_name else None
    if not raw:
        raw = cfg.get("oauth_client_secret_path") or ".secrets/google-drive-oauth-client.json"

    path = Path(str(raw)).expanduser()
    if not path.exists():
        raise GoogleDriveSourceError(
            f"Google Drive OAuth client JSON not found: {path}. "
            "Place it at .secrets/google-drive-oauth-client.json or set GOOGLE_DRIVE_OAUTH_CLIENT_SECRET."
        )
    if not path.is_file():
        raise GoogleDriveSourceError(f"Google Drive OAuth client path is not a file: {path}")
    return path


def _validate_oauth_client_json(path: Path) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        raise GoogleDriveSourceError(f"Could not read Google Drive OAuth client JSON: {path}") from e

    if "installed" in payload:
        block = payload["installed"] or {}
        missing = [k for k in ("client_id", "client_secret", "auth_uri", "token_uri") if not block.get(k)]
        if missing:
            raise GoogleDriveSourceError(
                f"Google Drive OAuth client JSON is missing installed.{', installed.'.join(missing)}"
            )
        return

    if "web" in payload:
        raise GoogleDriveSourceError(
            "Google Drive OAuth client JSON is a Web application client. "
            "This v1 local auth flow expects a Desktop app / installed client JSON."
        )

    raise GoogleDriveSourceError("Google Drive OAuth client JSON must contain an 'installed' block.")


def _token_path(cfg: dict) -> Path:
    return Path(str(cfg.get("token_path") or ".secrets/google-drive-token.json")).expanduser()


def resolve_google_drive_credentials(
    app_config: dict,
    *,
    oauth_client_secret_path: str | None = None,
    force_interactive: bool = False,
):
    """Resolve OAuth credentials, refreshing or launching browser auth when needed."""

    cfg = _drive_config(app_config)
    scopes = _drive_scopes(cfg)
    token_path = _token_path(cfg)
    creds = None

    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as e:
        raise GoogleDriveSourceError(
            "Google Drive OAuth dependencies are missing. Run `pip install -r requirements.txt`."
        ) from e

    if token_path.exists() and not force_interactive:
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), scopes)
        except Exception as e:
            logger.warning("Ignoring invalid Google Drive OAuth token file at %s: %s", token_path, e)
            creds = None

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token and not force_interactive:
        try:
            creds.refresh(Request())
            _save_credentials(creds, token_path)
            return creds
        except Exception as e:
            logger.warning("Google Drive OAuth token refresh failed; starting interactive auth: %s", e)
            creds = None

    client_path = _resolve_oauth_client_secret_path(cfg, oauth_client_secret_path)
    _validate_oauth_client_json(client_path)
    flow = InstalledAppFlow.from_client_secrets_file(str(client_path), scopes)
    creds = flow.run_local_server(port=0)
    _save_credentials(creds, token_path)
    return creds


def _save_credentials(creds: Any, token_path: Path) -> None:
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")


def build_google_drive_service(credentials, app_config: dict):
    try:
        from googleapiclient.discovery import build
    except ImportError as e:
        raise GoogleDriveSourceError(
            "Google Drive API dependency is missing. Run `pip install -r requirements.txt`."
        ) from e
    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def _parse_drive_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        normalized = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def _file_revision_id(file: dict) -> str | None:
    return (
        file.get("headRevisionId")
        or file.get("md5Checksum")
        or file.get("modifiedTime")
    )


def _drive_ref(file: dict, *, folder_id: str) -> DocumentRef:
    file_id = str(file.get("id") or "").strip()
    name = str(file.get("name") or "").strip()
    if not file_id or not name:
        raise GoogleDriveSourceError(f"Drive file response missing id or name: {file!r}")

    size_raw = file.get("size")
    try:
        size_bytes = int(size_raw) if size_raw is not None and str(size_raw).strip() else None
    except (TypeError, ValueError):
        size_bytes = None

    return DocumentRef(
        source_type="google_drive",
        display_name=name,
        uri=f"gdrive://{file_id}",
        source_id=file_id,
        revision_id=_file_revision_id(file),
        mime_type=file.get("mimeType"),
        size_bytes=size_bytes,
        modified_at=_parse_drive_datetime(file.get("modifiedTime")),
        metadata={
            "drive_folder_id": folder_id,
            "web_view_link": file.get("webViewLink"),
            "md5_checksum": file.get("md5Checksum"),
            "head_revision_id": file.get("headRevisionId"),
            "modified_time": file.get("modifiedTime"),
            "discovered_via": "google_drive_folder",
        },
    )


def discover_google_drive_documents(
    folder_id: str,
    app_config: dict,
    *,
    service=None,
) -> list[DocumentRef]:
    folder_id = str(folder_id).strip()
    if not folder_id:
        raise GoogleDriveSourceError("Google Drive folder ID is required.")

    cfg = _drive_config(app_config)
    if service is None:
        creds = resolve_google_drive_credentials(app_config)
        service = build_google_drive_service(creds, app_config)

    include_shared = bool(cfg.get("include_shared_drives", True))
    page_size = int(cfg.get("page_size") or 100)
    query = f"'{folder_id}' in parents and trashed = false and mimeType = '{DRIVE_PDF_MIME_TYPE}'"
    fields = (
        "nextPageToken,"
        "files(id,name,mimeType,size,modifiedTime,md5Checksum,headRevisionId,webViewLink)"
    )

    files: list[dict] = []
    page_token = None
    while True:
        try:
            request = service.files().list(
                q=query,
                pageSize=page_size,
                pageToken=page_token,
                fields=fields,
                supportsAllDrives=include_shared,
                includeItemsFromAllDrives=include_shared,
            )
            response = request.execute()
        except Exception as e:
            raise GoogleDriveSourceError(f"Could not list Google Drive folder {folder_id}: {e}") from e

        files.extend(response.get("files", []) or [])
        page_token = response.get("nextPageToken")
        if not page_token:
            break

    refs = [_drive_ref(f, folder_id=folder_id) for f in files]
    return sorted(refs, key=lambda r: r.display_name.lower())


def discover_google_drive_config_files(
    folder_id: str,
    app_config: dict,
    *,
    service=None,
) -> list[DocumentRef]:
    folder_id = str(folder_id).strip()
    if not folder_id:
        raise GoogleDriveSourceError("Google Drive config folder ID is required.")

    cfg = _drive_config(app_config)
    if service is None:
        creds = resolve_google_drive_credentials(app_config)
        service = build_google_drive_service(creds, app_config)

    include_shared = bool(cfg.get("include_shared_drives", True))
    page_size = int(cfg.get("page_size") or 100)
    query = f"'{folder_id}' in parents and trashed = false"
    fields = (
        "nextPageToken,"
        "files(id,name,mimeType,size,modifiedTime,md5Checksum,headRevisionId,webViewLink)"
    )

    files: list[dict] = []
    page_token = None
    while True:
        try:
            request = service.files().list(
                q=query,
                pageSize=page_size,
                pageToken=page_token,
                fields=fields,
                supportsAllDrives=include_shared,
                includeItemsFromAllDrives=include_shared,
            )
            response = request.execute()
        except Exception as e:
            raise GoogleDriveSourceError(f"Could not list Google Drive config folder {folder_id}: {e}") from e

        files.extend(response.get("files", []) or [])
        page_token = response.get("nextPageToken")
        if not page_token:
            break

    refs = [_drive_ref(f, folder_id=folder_id) for f in files]
    return sorted(refs, key=lambda r: r.display_name.lower())


def google_drive_source_hash(ref: DocumentRef) -> str:
    revision = (
        ref.revision_id
        or ref.metadata.get("md5_checksum")
        or ref.metadata.get("modified_time")
        or "unknown"
    )
    identity = f"google_drive:{ref.source_id}:{revision}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]


def _materialized_root(app_config: dict) -> Path:
    cfg = _drive_config(app_config)
    return Path(str(cfg.get("materialized_root") or "output/materialized")).expanduser()


def _config_materialized_root(app_config: dict) -> Path:
    cfg = _drive_config_folder_config(app_config)
    return Path(str(cfg.get("materialized_root") or "output/materialized/google_drive_config")).expanduser()


def _config_folder_revision_hash(refs: list[DocumentRef]) -> str:
    parts = []
    for ref in sorted(refs, key=lambda r: r.display_name.lower()):
        revision = (
            ref.revision_id
            or ref.metadata.get("md5_checksum")
            or ref.metadata.get("modified_time")
            or "unknown"
        )
        parts.append(f"{ref.display_name.lower()}:{ref.source_id}:{revision}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:12]


def _validate_config_refs(refs: list[DocumentRef]) -> dict[str, DocumentRef]:
    by_name: dict[str, DocumentRef] = {}
    duplicates: set[str] = set()
    for ref in refs:
        name = ref.display_name.strip()
        key = name.lower()
        if key not in ALL_CONFIG_FILES:
            continue
        if key in by_name:
            duplicates.add(key)
            continue
        by_name[key] = ref

    if duplicates:
        raise GoogleDriveSourceError(
            "Google Drive config folder contains duplicate config file(s): "
            + ", ".join(sorted(duplicates))
        )

    missing = sorted(REQUIRED_CONFIG_FILES - set(by_name))
    if missing:
        raise GoogleDriveSourceError(
            "Google Drive config folder is missing required file(s): "
            + ", ".join(missing)
        )

    return by_name


def materialize_google_drive_config_folder(
    app_config: dict,
    *,
    service=None,
) -> Path:
    folder_id = resolve_google_drive_config_folder_id(app_config)
    if service is None:
        creds = resolve_google_drive_credentials(app_config)
        service = build_google_drive_service(creds, app_config)

    refs = discover_google_drive_config_files(folder_id, app_config, service=service)
    by_name = _validate_config_refs(refs)
    selected_refs = list(by_name.values())
    revision_hash = _config_folder_revision_hash(selected_refs)
    out_dir = _config_materialized_root(app_config) / folder_id / revision_hash
    out_dir.mkdir(parents=True, exist_ok=True)

    for name, ref in by_name.items():
        if not ref.source_id:
            raise GoogleDriveSourceError(f"Google Drive config file is missing source_id: {ref.display_name}")
        final_path = out_dir / name
        part_path = out_dir / f"{name}.part"
        try:
            request = service.files().get_media(fileId=ref.source_id, supportsAllDrives=True)
            _download_media_to_path(request, part_path)
            part_path.replace(final_path)
        except Exception as e:
            try:
                part_path.unlink()
            except FileNotFoundError:
                pass
            raise GoogleDriveSourceError(f"Could not download Google Drive config file {ref.display_name}: {e}") from e

    logger.info("Loaded Google Drive config folder %s into %s", folder_id, out_dir)
    return out_dir


def materialize_google_drive_document(
    ref: DocumentRef,
    app_config: dict,
    *,
    service=None,
) -> MaterializedDocument:
    if ref.source_type != "google_drive":
        raise GoogleDriveSourceError(f"Cannot materialize non-Drive source: {ref.source_type}")
    if not ref.source_id:
        raise GoogleDriveSourceError("Google Drive source_id is required for download.")
    if ref.mime_type != DRIVE_PDF_MIME_TYPE:
        raise GoogleDriveSourceError(f"Google Drive source is not a PDF: {ref.display_name}")

    if service is None:
        creds = resolve_google_drive_credentials(app_config)
        service = build_google_drive_service(creds, app_config)

    source_hash = google_drive_source_hash(ref)
    provenance = SourceProvenance(
        source_type="google_drive",
        source_id=ref.source_id,
        source_uri=ref.uri,
        display_name=ref.display_name,
        original_filename=ref.display_name,
        revision_id=ref.revision_id,
        source_hash=source_hash,
        content_sha256=None,
        materialization_method="download",
        metadata={
            **ref.metadata,
            "size_bytes": ref.size_bytes,
        },
    )
    run_identity = build_run_identity(ref, provenance)
    safe_name = safe_document_stem(ref.display_name)
    out_dir = _materialized_root(app_config) / "google_drive" / run_identity.run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    final_path = out_dir / f"{safe_name}.pdf"
    part_path = out_dir / f"{safe_name}.pdf.part"

    try:
        request = service.files().get_media(fileId=ref.source_id, supportsAllDrives=True)
        _download_media_to_path(request, part_path)
        part_path.replace(final_path)
    except Exception as e:
        try:
            part_path.unlink()
        except FileNotFoundError:
            pass
        raise GoogleDriveSourceError(f"Could not download Google Drive file {ref.display_name}: {e}") from e

    provenance.content_sha256 = content_sha256(final_path)
    provenance.metadata["materialized_path"] = str(final_path)
    return MaterializedDocument(
        ref=ref,
        local_pdf_path=str(final_path),
        provenance=provenance,
        run_identity=run_identity,
    )


def _download_media_to_path(request, path: Path) -> None:
    try:
        from googleapiclient.http import MediaIoBaseDownload
    except ImportError as e:
        raise GoogleDriveSourceError(
            "Google Drive API dependency is missing. Run `pip install -r requirements.txt`."
        ) from e

    with io.FileIO(path, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            _status, done = downloader.next_chunk()


def materialize_google_drive_input(
    folder_id: str,
    app_config: dict,
    *,
    oauth_client_secret_path: str | None = None,
) -> list[MaterializedDocument]:
    creds = resolve_google_drive_credentials(
        app_config,
        oauth_client_secret_path=oauth_client_secret_path,
    )
    service = build_google_drive_service(creds, app_config)
    refs = discover_google_drive_documents(folder_id, app_config, service=service)
    return [materialize_google_drive_document(ref, app_config, service=service) for ref in refs]


def cleanup_materialized_google_drive_document(doc: MaterializedDocument) -> None:
    if doc.ref.source_type != "google_drive":
        return
    path = Path(doc.local_pdf_path)
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except Exception as e:
        logger.warning("Could not delete materialized Google Drive PDF %s: %s", path, e)
        return

    parent = path.parent
    try:
        parent.rmdir()
    except OSError:
        pass


def google_drive_output_dir(doc: MaterializedDocument, base_output: str | Path) -> Path:
    return Path(base_output) / f"{doc.run_identity.safe_document_stem}-{doc.run_identity.source_hash}"


def google_drive_cleanup_enabled(app_config: dict) -> bool:
    cfg = _drive_config(app_config)
    return bool(cfg.get("cleanup_downloads", True))
