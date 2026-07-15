# External Integrations

**Analysis Date:** 2026-06-25

## APIs & External Services

**LLM Providers:**
- Ollama - local text and vision model backend.
  - SDK/Client: direct HTTP requests in `src/llm/ollama_provider.py`.
  - Auth: none for default local server.
- Gemini Developer API - remote text and vision backend.
  - SDK/Client: `google-genai` in `src/llm/gemini_provider.py`.
  - Auth: environment variable named by `gemini.api_key_env`, default `GOOGLE_API_KEY`.
- OpenAI API - remote text and vision backend.
  - SDK/Client: `openai` in `src/llm/openai_provider.py`.
  - Auth: environment variable named by `openai.api_key_env`, default `OPENAI_API_KEY`.

**Google Drive:**
- Google Drive API v3 - invoice source and optional CSV config source.
  - SDK/Client: `google-api-python-client`, `google-auth`, and `google-auth-oauthlib` in `src/sources/google_drive.py`.
  - Auth: desktop OAuth client JSON plus token file configured under `sources.google_drive` in `config/config.yaml`.

## Data Storage

**Databases:**
- Not detected. The app uses filesystem storage, CSV configuration, and in-memory state.

**File Storage:**
- Local filesystem for input PDFs in `invoices/`, output CSV/log artifacts under `output/`, and learning notes in `learnings/learnings.md`.
- Google Drive for remote source PDFs and optional remote CSV config folder; files are materialized under `output/materialized` by `src/sources/google_drive.py`.

**Caching:**
- Pydantic schema cache in `ConfigStore.schema_cache` within `src/config/loader.py`.
- OAuth token persistence at `sources.google_drive.token_path`.
- Model/package caches are external to the repo, for example Surya/transformer model caches.

## Authentication & Identity

**Auth Provider:**
- Google OAuth installed-app flow for Drive ingestion and config-folder materialization.
  - Implementation: `resolve_google_drive_credentials()` in `src/sources/google_drive.py` loads, refreshes, or creates token credentials.
- API-key auth for Gemini/OpenAI providers.
  - Implementation: `src/llm/config_resolve.py` resolves configured env vars; `src/llm/factory.py` wraps remote providers with `MeteredLLMProvider` when guardrails are active.

## Monitoring & Observability

**Error Tracking:**
- No external error tracker detected.

**Logs:**
- Python `logging` with configurable level in `config/config.yaml`.
- Run-level JSONL logging through `src/agent/loop_utils.py` and tool result logging in `src/agent/agent.py` / `src/agent/pipeline.py`.
- Human-readable live presentation output through `src/output/presenter.py`.

## CI/CD & Deployment

**Hosting:**
- No hosted service configuration detected. Runtime is local CLI or Docker container.

**CI Pipeline:**
- GitHub Actions workflow `.github/workflows/ci.yml` installs `requirements.txt` and runs `pytest tests/ -q`.

## Environment Configuration

**Required env vars:**
- `OPENAI_API_KEY` when `llm.provider: openai`.
- `GOOGLE_API_KEY` when `llm.provider: gemini`.
- `GOOGLE_DRIVE_OAUTH_CLIENT_SECRET` optionally overrides the Drive OAuth client JSON path.

**Secrets location:**
- `.env` for local API keys; `.env` is gitignored.
- `.secrets/google-drive-oauth-client.json` and `.secrets/google-drive-token.json` for Drive OAuth; `.secrets/` is gitignored.

## Webhooks & Callbacks

**Incoming:**
- None detected. This is a CLI/container workflow.

**Outgoing:**
- Google OAuth local browser callback handled by `InstalledAppFlow.run_local_server()` in `src/sources/google_drive.py`.
- LLM API requests to configured providers.
- Google Drive API list/download calls.

---

*Integration audit: 2026-06-25*
