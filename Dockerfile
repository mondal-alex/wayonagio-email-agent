# syntax=docker/dockerfile:1.7
#
# Cloud Run-ready image for the Wayonagio Email Agent API.
#
# - Runs `uvicorn wayonagio_email_agent.api:app` bound to the port Cloud Run
#   injects via the PORT env var (default 8080 for local `docker run`).
# - Uses uv for fast, reproducible dependency installs from `uv.lock`.
# - Runs as a non-root user.
#
# Build (amd64 is what Cloud Run runs):
#   docker buildx build --platform linux/amd64 -t wayonagio-email-agent:latest .
#
# Run locally:
#   docker run --rm -p 8080:8080 \
#     --env-file .env \
#     -v "$PWD/credentials.json:/app/credentials.json:ro" \
#     -v "$PWD/token.json:/app/token.json:ro" \
#     wayonagio-email-agent:latest

# ---------- Stage 1: build venv with uv ------------------------------------
FROM python:3.13-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

# Install only runtime deps first (cacheable layer).
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# Now copy the source and install the project itself.
COPY src ./src
RUN uv sync --frozen --no-dev

# ---------- Stage 2: slim runtime ------------------------------------------
FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    PORT=8080

RUN groupadd --system app && useradd --system --gid app --home-dir /app app

WORKDIR /app

COPY --from=builder /app /app
RUN chown -R app:app /app

USER app

EXPOSE 8080

# Cloud Run injects PORT; we honor it. Bind to 0.0.0.0 so the container is
# reachable from outside. Single worker is correct for Cloud Run (autoscaling
# happens at the instance level, not inside the container).
CMD ["sh", "-c", "exec uvicorn wayonagio_email_agent.api:app --host 0.0.0.0 --port ${PORT}"]
