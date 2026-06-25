# Technology Stack

**Analysis Date:** 2026-06-25

## Languages

**Primary:**
- Python 3.11 - CLI application, agent orchestration, PDF/image processing, LLM provider adapters, Google Drive ingestion, output generation, and tests.

**Secondary:**
- YAML - Runtime configuration in `config/config.yaml` and Docker-specific configuration in `config/config.docker-ollama.yaml`.
- CSV - Business configuration in `config/csv/*.csv` for invoice types, extraction fields, compliance rules, allowed values, and ground truth.
- HTML - Demo/presentation artifact in `presentation.html`; this is not an application frontend.

## Runtime

**Environment:**
- Python 3.11, pinned by `Dockerfile` via `python:3.11-slim-bookworm` and by CI setup in `.github/workflows/ci.yml`.
- Local development is documented for Conda and direct `pip install -r requirements.txt` in `README.md`.

**Package Manager:**
- pip with `requirements.txt`.
- Lockfile: missing; dependency versions are lower bounds/ranges, not fully locked.

## Frameworks

**Core:**
- Pydantic 2 - typed models for config, agent state, actions, and tool I/O in `src/models/*.py` and `src/agent/state.py`.
- PyYAML - app config loading in `main.py` and `src/config/loader.py`.
- Rich - live/demo presenter implementation in `src/output/presenter.py`.

**AI / OCR / Document Processing:**
- `openai` - remote OpenAI provider in `src/llm/openai_provider.py`.
- `google-genai` - Gemini provider in `src/llm/gemini_provider.py`.
- Ollama HTTP API via `requests` - local provider in `src/llm/ollama_provider.py`.
- `surya-ocr` and `transformers` - OCR pre-pass and model loading in `src/tools/ocr_layout.py`.
- `pypdfium2` and Pillow - PDF rendering, compression, crops, and base64 image preparation in `src/tools/pdf_pages.py`.

**Testing:**
- pytest 7+ - test runner across `tests/`.

**Build/Dev:**
- Docker - local image in `Dockerfile`.
- Docker Compose - local Gemini and Ollama profiles in `docker-compose.yml`.
- GitHub Actions - CI test workflow in `.github/workflows/ci.yml`.

## Key Dependencies

**Critical:**
- `pydantic>=2.0.0` - strict domain models and validation.
- `pypdfium2>=4.30.0` and `Pillow>=10.0.0` - PDF-to-image processing.
- `surya-ocr` - OCR text and layout hints before vision extraction.
- `google-genai>=1.0.0` and `openai>=1.0.0` - remote LLM backends.
- `google-api-python-client`, `google-auth`, `google-auth-oauthlib` - Google Drive OAuth and file download/config-folder ingestion.
- `requests>=2.31.0` - Ollama provider HTTP calls.

**Infrastructure:**
- `python-dotenv>=1.0.0` - `.env` loading in `main.py`.
- `PyYAML>=6.0` - YAML config parsing.
- `rich>=13.0.0` - presentation-mode terminal UI.
- `pytest>=7.0` - test suite.

## Configuration

**Environment:**
- `.env.example` documents API-key style setup; `.env` is loaded from the project root and current working directory by `main.py`.
- `config/config.yaml` chooses `llm.provider`, model IDs, timeouts, guardrails, source settings, output location, OCR languages, and agent loop behavior.
- Do not read or commit `.env`, `.secrets/`, OAuth token files, or downloaded invoice output.

**Build:**
- `Dockerfile` builds a Python 3.11 image, installs system libraries required by image/OCR packages, installs `requirements.txt`, copies `main.py`, `src/`, `config/`, and `learnings/`, then preloads Surya models.
- `docker-compose.yml` exposes `agent` for Gemini and `agent-ollama` plus `ollama` for local GPU-backed runs.
- `.github/workflows/ci.yml` installs requirements and runs `pytest tests/ -q` on pushes to `main` and pull requests.

## Platform Requirements

**Development:**
- Python 3.11.
- Populated `config/csv/` files.
- Optional local Ollama server at `http://localhost:11434` for `llm.provider: ollama`.
- Optional API keys for `gemini` or `openai`, supplied via environment variables configured in `config/config.yaml`.
- Optional Google Drive OAuth desktop-client JSON at `.secrets/google-drive-oauth-client.json` or path override.

**Production:**
- Current deployment shape is CLI/container based, not a web service.
- Docker image expects writable `invoices`, `output`, and `learnings` directories.
- Remote-provider runs should keep `llm.remote_guard` enabled to cap request/token use.

---

*Stack analysis: 2026-06-25*
