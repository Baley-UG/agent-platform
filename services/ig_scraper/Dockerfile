FROM python:3.13.2-slim

WORKDIR /app

ARG APP_ENV=production

ENV APP_ENV=${APP_ENV} \
    PYTHONFAULTHANDLER=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONHASHSEED=random \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=on \
    PIP_DEFAULT_TIMEOUT=100 \
    PYTHONPATH=/app

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        curl \
        ca-certificates \
    && pip install --upgrade pip \
    && pip install uv \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps first (cache layer)
COPY pyproject.toml .
RUN uv venv && . .venv/bin/activate && uv pip install -e .

# Copy application code
COPY . .

RUN useradd -m appuser && chown -R appuser:appuser /app
USER appuser

RUN mkdir -p /app/logs

EXPOSE 8081

# Default command runs the API. Override with `python -m app.worker` /
# `python -m app.scheduler` for the other process roles.
CMD ["/app/.venv/bin/uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8081"]
