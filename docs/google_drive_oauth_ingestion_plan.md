# Google Drive OAuth Invoice Ingestion Plan

## Summary

Implement Google Drive invoice loading using OAuth as the first working authentication path. The feature will let the agent authenticate with a normal Google account, list PDF invoices from a Drive folder, download each PDF into a temporary local materialization area, process it through the existing invoice pipeline, and delete the downloaded PDF afterward.

The implementation should keep OAuth behind a small credential-resolution boundary so future production auth, such as Application Default Credentials on Cloud Run or Workload Identity Federation on external hosting, can be added without rewriting Drive discovery, materialization, or invoice processing.

Current local secret convention:

- OAuth client JSON: `.secrets/google-drive-oauth-client.json`
- OAuth token JSON: `.secrets/google-drive-token.json`
- Both paths are ignored by Git.

## Goals

- Add Google Drive as a new document source beside local PDF files.
- Support folder-batch ingestion of Google Drive PDF files.
- Use OAuth locally with the downloaded `installed` OAuth client JSON.
- Persist and refresh OAuth tokens automatically.
- Preserve source provenance for Drive files.
- Avoid local storage pollution by deleting downloaded Drive PDFs after processing.
- Keep existing local `--pdf` behavior unchanged.
- Design the auth boundary so later service-account or ADC support is a credential-provider addition, not a Drive-source rewrite.

## Non-Goals For V1

- No Google Docs export support.
- No recursive folder traversal.
- No Drive writes, uploads, moves, labels, or processed-state updates.
- No domain-wide delegation.
- No service-account JSON keys.
- No deletion of normal agent outputs, logs, rendered pages, crops, or CSVs.

## Dependencies

Update `requirements.txt` with:

```text
google-api-python-client>=2.0.0
google-auth>=2.0.0
google-auth-oauthlib>=1.0.0
```

Use Google client libraries for OAuth and Drive API behavior rather than hand-rolled HTTP auth.

## Configuration

Use the existing `sources.google_drive` config block:

```yaml
sources:
  google_drive:
    auth_mode: oauth
    scopes:
      - https://www.googleapis.com/auth/drive
    oauth_client_secret_path: ".secrets/google-drive-oauth-client.json"
    oauth_client_secret_path_env: GOOGLE_DRIVE_OAUTH_CLIENT_SECRET
    token_path: ".secrets/google-drive-token.json"
    materialized_root: "output/materialized"
    cleanup_downloads: true
    include_shared_drives: true
    page_size: 100
```

Notes:

- The Drive scope is intentionally write-capable even though v1 only reads, because write behavior is expected soon and this avoids forcing an immediate re-consent.
- `GOOGLE_DRIVE_OAUTH_CLIENT_SECRET` should override `oauth_client_secret_path`.
- `token_path` parent directories must be created automatically.
- Secrets and token files must never be logged.

## CLI Interface

Add these flags to `main.py`:

```text
--google-drive-folder-id <ID>
--drive-auth
--drive-oauth-client-secret <PATH>
```

Behavior:

- `--drive-auth` authenticates, saves/refreshes the OAuth token, prints a short success message, and exits without running the invoice agent.
- `--google-drive-folder-id <ID>` discovers and processes PDFs from that Drive folder.
- `--drive-oauth-client-secret <PATH>` overrides config/env client-secret resolution.
- Existing `--pdf` behavior remains unchanged.
- Reject `--pdf` and `--google-drive-folder-id` when both are explicitly provided.
- If neither Drive flag is provided, continue using existing local `--pdf` default.
- If Drive discovery finds no PDFs, print a clear message and exit successfully without running the agent.

## Source Module

Add `src/sources/google_drive.py`.

Core API:

```python
class GoogleDriveSourceError(ValueError):
    ...

def resolve_google_drive_credentials(
    app_config: dict,
    *,
    oauth_client_secret_path: str | None = None,
    force_interactive: bool = False,
):
    ...

def build_google_drive_service(credentials, app_config: dict):
    ...

def discover_google_drive_documents(
    folder_id: str,
    app_config: dict,
    *,
    service=None,
) -> list[DocumentRef]:
    ...

def materialize_google_drive_document(
    ref: DocumentRef,
    app_config: dict,
    *,
    service=None,
) -> MaterializedDocument:
    ...

def materialize_google_drive_input(
    folder_id: str,
    app_config: dict,
    *,
    oauth_client_secret_path: str | None = None,
) -> list[MaterializedDocument]:
    ...

def cleanup_materialized_google_drive_document(doc: MaterializedDocument) -> None:
    ...

def google_drive_output_dir(doc: MaterializedDocument, base_output: str | Path) -> Path:
    ...
```

Keep OAuth credential resolution separate from Drive file operations. This makes later `auth_mode: adc` or `auth_mode: workload_identity` easier to add.

## OAuth Credential Resolution

Implement OAuth with `google_auth_oauthlib.flow.InstalledAppFlow`.

Resolution order for client JSON:

1. CLI `--drive-oauth-client-secret`
2. Env var named by `sources.google_drive.oauth_client_secret_path_env`
3. Config `sources.google_drive.oauth_client_secret_path`

Token behavior:

- Load token from `sources.google_drive.token_path` if it exists.
- If credentials are valid, use them.
- If credentials are expired and have a refresh token, refresh them automatically.
- If credentials are missing, invalid, or cannot be refreshed, run browser OAuth flow.
- Save new/refreshed credentials back to `token_path`.
- Create the token directory if missing.
- Emit clear errors for missing client JSON, malformed JSON, missing `installed`/`web` block, or missing required OAuth fields.

Implementation details:

- Support the current `installed` JSON shape.
- If a `web` OAuth client JSON is encountered, raise a clear message that v1 local auth expects an installed/Desktop client unless server callback support has been implemented.
- Do not print client secrets, access tokens, or refresh tokens.

## Drive Discovery

List direct PDF children of the provided folder ID.

Drive query:

```text
'<folder_id>' in parents and trashed = false and mimeType = 'application/pdf'
```

Use these API options:

- `pageSize` from config.
- `supportsAllDrives=True` when `include_shared_drives` is true.
- `includeItemsFromAllDrives=True` when `include_shared_drives` is true.
- Continue through `nextPageToken`.

Request only necessary fields:

```text
nextPageToken,
files(id,name,mimeType,size,modifiedTime,md5Checksum,headRevisionId,webViewLink)
```

Return `DocumentRef` values sorted by display name, case-insensitive.

`DocumentRef` mapping:

- `source_type="google_drive"`
- `display_name=file["name"]`
- `uri=f"gdrive://{file['id']}"`
- `source_id=file["id"]`
- `revision_id=headRevisionId or md5Checksum or modifiedTime`
- `mime_type=file["mimeType"]`
- `size_bytes=int(size)` when present
- `modified_at=modifiedTime` parsed as timezone-aware UTC datetime
- `metadata` includes:
  - `drive_folder_id`
  - `web_view_link`
  - `md5_checksum`
  - `head_revision_id`
  - `discovered_via="google_drive_folder"`

## Materialization

The agent already expects a local PDF path, so Drive files must be downloaded before processing.

Materialization behavior:

- Build source hash from stable Drive identity:
  - `google_drive:<file_id>:<revision_id or md5Checksum or modifiedTime>`
- Build `RunIdentity` with existing `build_run_identity`.
- Store downloads under:

```text
output/materialized/google_drive/<run_id>/<safe-document-stem>.pdf
```

- Download via `service.files().get_media(fileId=...)`.
- Stream to a temporary `.part` file first.
- Atomically rename/move to the final PDF path after the download completes.
- Compute `content_sha256` from the local file.
- Build `SourceProvenance`:
  - `source_type="google_drive"`
  - `source_id=<Drive file ID>`
  - `source_uri="gdrive://<Drive file ID>"`
  - `display_name=<Drive filename>`
  - `original_filename=<Drive filename>`
  - `revision_id=<Drive revision/checksum/time>`
  - `source_hash=<Drive source hash>`
  - `content_sha256=<downloaded content hash>`
  - `materialization_method="download"`
  - `metadata` carries the Drive metadata from the ref plus materialized local path details.

## Output Directory Behavior

Local source output behavior stays unchanged.

Drive source output directories must avoid filename collisions:

```text
output/<safe_document_stem>-<source_hash>
```

Add `google_drive_output_dir(doc, base_output)` and use it only for Drive documents.

## Cleanup

Drive downloads are temporary.

Cleanup behavior:

- Wrap every Drive document processing call in `try/finally`.
- In `finally`, call `cleanup_materialized_google_drive_document(doc)` when `cleanup_downloads` is true.
- Delete only `doc.local_pdf_path` for `source_type="google_drive"`.
- Remove the empty per-run materialized folder after deleting the PDF.
- Ignore missing files during cleanup.
- Log cleanup failures as warnings without masking the processing failure.
- Never delete:
  - local source PDFs
  - output CSVs
  - run logs
  - rendered pages
  - crops
  - agent temporary output artifacts

## CLI Processing Flow

Update `main.py` flow:

1. Parse source flags.
2. Load `.env`, config, logging, and CSV store as today.
3. If `--drive-auth`:
   - Resolve OAuth credentials with `force_interactive=True`.
   - Print token path and granted scope summary.
   - Exit.
4. If `--google-drive-folder-id`:
   - Materialize Drive input.
   - If no PDFs, print and exit.
   - Process as batch.
   - Use Drive output directory helper.
   - Cleanup each materialized Drive PDF in `finally`.
5. Else:
   - Run existing local materialization and processing unchanged.

Do not mix local and Drive batches in v1.

## README Updates

Add a Google Drive section covering:

- Enable Google Drive API.
- Create OAuth consent screen.
- Create OAuth client of type Desktop app.
- Rename downloaded JSON to:

```text
.secrets/google-drive-oauth-client.json
```

- Authenticate:

```bash
python main.py --drive-auth
```

- Process a folder:

```bash
python main.py --google-drive-folder-id <folder-id>
```

- Explain token behavior:
  - access tokens refresh automatically
  - Testing-mode OAuth apps may need re-auth after 7 days
  - moving consent screen to production makes the flow more stable
- Explain cleanup:
  - downloaded Drive PDFs are deleted after processing
  - outputs/logs are preserved
- Explain future production path:
  - same Drive source can later use ADC/service-account auth without changing invoice processing.

## Tests

Add `tests/test_sources_google_drive.py`.

Test credential behavior with mocks:

- Loads valid token from token path.
- Refreshes expired token and writes updated token.
- Runs installed-app flow when token is missing.
- Creates token parent directory.
- Uses CLI override before env/config path.
- Raises clear error for missing OAuth client JSON.

Test discovery:

- Lists only PDFs from Drive folder.
- Handles empty folders.
- Handles pagination.
- Passes Shared Drive flags.
- Builds expected `DocumentRef` fields and metadata.

Test materialization:

- Streams bytes to local PDF path.
- Writes through `.part` file then final path.
- Computes SHA-256.
- Builds `google_drive` provenance.
- Builds stable source hash from Drive metadata.
- Builds collision-safe output dir.

Test cleanup:

- Deletes Drive materialized PDF after success.
- Deletes Drive materialized PDF after processing failure.
- Removes empty materialized run folder.
- Does not delete local PDFs.
- Does not delete output CSV/log directories.

Update `tests/test_cli_sources.py`:

- `--drive-auth` authenticates and exits without constructing `InvoiceAgent`.
- `--google-drive-folder-id` routes through Drive materialization.
- Drive batch calls `process_invoice` with Drive provenance and run identity.
- Conflicting explicit `--pdf` and `--google-drive-folder-id` errors clearly.
- Existing local single-file and folder-batch tests still pass.

Run at minimum:

```bash
pytest tests/test_sources_google_drive.py tests/test_cli_sources.py tests/test_sources_local.py tests/test_run_identity.py
```

Then run the full suite if dependency installation and environment allow:

```bash
pytest
```

## Manual Acceptance

1. Put OAuth client JSON at:

```text
.secrets/google-drive-oauth-client.json
```

2. Authenticate:

```bash
python main.py --drive-auth
```

3. Process a Drive folder containing at least one PDF:

```bash
python main.py --google-drive-folder-id <folder-id>
```

4. Confirm:

- Browser login happens only when token is missing/invalid.
- PDFs are discovered from Drive.
- Each PDF is processed with existing invoice logic.
- Outputs are written under collision-safe output folders.
- Run logs include Drive provenance.
- Downloaded PDFs are removed from `output/materialized`.
- Normal outputs remain available.

## Risks And Guardrails

- OAuth apps in Testing can produce refresh tokens that expire after 7 days for Drive scopes. Document this and make reconnect easy with `--drive-auth`.
- The selected Drive scope is broad. Keep it configurable and avoid any Drive write calls in v1.
- OAuth refresh tokens are sensitive. Never log them and keep token files gitignored.
- Drive API errors should be surfaced clearly, especially permission errors, missing folder access, and disabled Drive API.
- Cleanup must never use broad directory deletion. Delete the exact materialized PDF path and remove only empty parent directories.

