# Invoice Compliance Agent — local CLI and (Phase 2) AWS worker image.
# Expect ~3–5 GB with surya-ocr + PyTorch CPU wheels on first build.

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
    && pip install -r requirements.txt \
    && pip install "transformers>=4.56.1,<5"

COPY main.py ./
COPY src/ src/
COPY config/ config/
COPY learnings/ learnings/

RUN mkdir -p invoices output \
    && python -c "from src.tools.ocr_layout import load_surya_models; assert load_surya_models() is not None"

RUN useradd -m -u 1000 appuser \
    && chown -R appuser:appuser /app
USER appuser

CMD ["python", "main.py", "--help"]
