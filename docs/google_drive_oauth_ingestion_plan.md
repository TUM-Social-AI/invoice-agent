# Google Drive OAuth Invoice Ingestion Plan

## Summary

Google Drive invoice ingestion began as an OAuth-based source for local browser authentication. The Drive source lists PDF invoices from a configured folder, downloads each PDF into a temporary local materialization area, processes it through the existing invoice pipeline, and deletes the downloaded PDF afterward while preserving normal outputs and logs.

The credential-resolution boundary now supports both OAuth and service-account credentials. OAuth remains supported for local personal-account use, while `sources.google_drive.auth_mode: service_account` supports non-browser local, Docker, CI, and AWS-style deployments.

## Current Credential Boundary

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
    service_account_file: ".secrets/google-drive-service-account.json"
    service_account_file_env: GOOGLE_DRIVE_SERVICE_ACCOUNT_FILE
    service_account_json_env: GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON
```

OAuth mode still resolves the installed-app client JSON, refreshes tokens, and launches the browser flow when required. Service-account mode resolves credentials from `GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON`, `GOOGLE_DRIVE_SERVICE_ACCOUNT_FILE`, `sources.google_drive.service_account_file`, or the default `.secrets/google-drive-service-account.json` path.

## Service-Account Extension

The original OAuth credential boundary has been extended with `service_account` mode rather than replaced. This keeps Drive discovery, materialization, provenance, and cleanup behavior independent from the selected credential provider.

Service-account mode is intended for non-interactive deployments:

- Local development can mount `.secrets/google-drive-service-account.json`.
- Docker can mount `./.secrets:/app/.secrets:ro` and set `GOOGLE_DRIVE_SERVICE_ACCOUNT_FILE=/app/.secrets/google-drive-service-account.json`.
- AWS deployments can store the JSON key in Secrets Manager or SSM Parameter Store and inject the full secret text into `GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON`, or mount a managed secret file and set `GOOGLE_DRIVE_SERVICE_ACCOUNT_FILE`.

The app does not need direct AWS SDK access for this phase because the runtime environment supplies the secret as an environment variable or file.

## Drive Sharing Requirement

A service account is a separate Google identity. The invoice Drive folder and any Drive-backed config folders/files must be shared with the service account's `client_email` before the app can list or download them. Share only the required folders/files, not broad personal Drive access.

## Security Notes

- Do not commit OAuth client secrets, OAuth tokens, or service-account JSON keys.
- Keep local secret files under `.secrets/`.
- Do not bake service-account JSON keys into Docker images.
- Do not print raw JSON keys, private keys, access tokens, or refresh tokens in logs.

## OAuth Compatibility

`auth_mode: oauth` remains the default-compatible path for existing local users. `python main.py --drive-auth` still creates or refreshes OAuth credentials in OAuth mode; in service-account mode it validates the configured service-account credentials non-interactively.
