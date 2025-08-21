# Production Dockerfile for FastAPI/uvicorn on DO App Platform
FROM python:3.11-slim

# --- Runtime env hygiene + defaults ---
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080 \
    UVICORN_WORKERS=1

# --- OS deps (TLS roots + curl for HEALTHCHECK) ---
RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates curl && \
    rm -rf /var/lib/apt/lists/*

# --- Non-root user ---
RUN useradd -ms /bin/bash appuser

WORKDIR /app

# --- Install Python deps (single pass, cached by requirements.txt) ---
COPY requirements.txt .
RUN python -m pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# --- App code ---
COPY --chown=appuser:appuser backend ./backend

USER appuser

EXPOSE 8080

# Local container healthcheck (DO uses its own HTTP check, but this helps locally)
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/health" || exit 1

# NOTE: keep workers=1 so your background agents/migrations don't multiply per process
CMD sh -c 'uvicorn backend.main:app --host 0.0.0.0 --port ${PORT:-8080} --workers ${UVICORN_WORKERS}'
