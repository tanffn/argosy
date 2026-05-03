# Argosy engine multi-stage Dockerfile (Phase 6).
#
# stage `builder` -> installs deps via uv.
# stage `runtime` -> copies the built site-packages + the package.
#
# Build:
#   docker build -t argosy-engine:latest .
# Run (self-hosted):
#   docker run --rm -p 8000:8000 \
#     -e ARGOSY_HOME=/data \
#     -v $(pwd)/argosy_home:/data \
#     argosy-engine:latest

# ----------------------------------------------------------------------
# Builder
# ----------------------------------------------------------------------
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# Install uv (fast Python package manager).
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates build-essential \
    && rm -rf /var/lib/apt/lists/* \
    && curl -LsSf https://astral.sh/uv/install.sh | sh

ENV PATH="/root/.local/bin:${PATH}"

WORKDIR /build
COPY pyproject.toml uv.lock README.md ./
COPY argosy ./argosy
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini
COPY argosy.toml ./argosy.toml

RUN uv sync --frozen --no-dev

# ----------------------------------------------------------------------
# Runtime
# ----------------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ARGOSY_HOME=/data

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1001 argosy

COPY --from=builder /build/.venv /opt/argosy-venv
COPY --from=builder /build/argosy /app/argosy
COPY --from=builder /build/alembic /app/alembic
COPY --from=builder /build/alembic.ini /app/alembic.ini
COPY --from=builder /build/argosy.toml /app/argosy.toml

ENV PATH="/opt/argosy-venv/bin:${PATH}" \
    PYTHONPATH="/app"

WORKDIR /app
USER argosy

EXPOSE 8000

# Healthcheck hits /health; the FastAPI route returns ok / db: ok.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; \
        sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status==200 else 1)"

CMD ["uvicorn", "argosy.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
