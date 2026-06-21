# Invoice Compliance Agent — local CLI and (Phase 2) AWS worker image.
# OCR backend is selected in config/config.yaml.

FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --upgrade pip \
    && pip install -r requirements.txt

COPY main.py ./
COPY src/ src/
COPY config/ config/
COPY learnings/ learnings/

RUN useradd -m -u 1000 appuser \
    && chown -R appuser:appuser /app
USER appuser

RUN mkdir -p invoices output \
    && python -c "import yaml; from src.tools.ocr_layout import load_ocr_engine; cfg=yaml.safe_load(open('config/config.yaml', encoding='utf-8')); load_ocr_engine(cfg)"

CMD ["python", "main.py", "--help"]
