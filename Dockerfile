# ─────────────────────────────────────────────────────────────────────────────
# Stage 1: builder – install dependencies into a virtual environment
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build

# System deps needed to compile psycopg binary wheel + openpyxl C ext
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy only dependency manifests first – maximises layer cache hits
COPY pyproject.toml ./
# Hatch needs a README to parse metadata; supply a stub if absent
RUN touch README.md

# Create venv and install into it
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

RUN pip install --upgrade pip && \
    pip install hatchling && \
    pip install -e ".[dev]"

# ─────────────────────────────────────────────────────────────────────────────
# Stage 2: runtime – minimal image, copy venv from builder
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

# Non-root user for security
RUN useradd --system --create-home --uid 1001 appuser

WORKDIR /app

# Runtime system libraries only (no compiler)
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Copy venv from builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy application source
COPY --chown=appuser:appuser rfp_responder ./rfp_responder

# Alembic migrations
COPY --chown=appuser:appuser alembic.ini ./alembic.ini
COPY --chown=appuser:appuser migrations ./migrations

USER appuser

# Uvicorn defaults – overridden by docker-compose / Kubernetes env
ENV APP_ENV=production \
    LOG_LEVEL=INFO \
    PORT=8000

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:${PORT}/health || exit 1

# Number of workers auto-detected from CPU count at runtime via shell arithmetic
CMD ["sh", "-c", "uvicorn rfp_responder.app.main:app --host 0.0.0.0 --port ${PORT} --workers ${UVICORN_WORKERS:-2} --loop uvloop --access-log"]
