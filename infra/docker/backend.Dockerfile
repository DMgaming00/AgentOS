# Single image for the API, worker and migration jobs.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv PATH=/app/.venv/bin:$PATH
RUN pip install --no-cache-dir uv==0.13.5
WORKDIR /app

# Dependencies first for layer caching.
COPY pyproject.toml uv.lock ./
COPY packages/shared/pyproject.toml packages/shared/pyproject.toml
COPY apps/api/pyproject.toml apps/api/pyproject.toml
COPY apps/worker/pyproject.toml apps/worker/pyproject.toml
RUN uv sync --frozen --no-dev --no-install-workspace

COPY packages packages
COPY apps/api apps/api
COPY apps/worker apps/worker
COPY examples examples
RUN uv sync --frozen --no-dev

RUN useradd --uid 10001 --create-home agentos && mkdir -p /data/artifacts && chown -R agentos /data
USER agentos
