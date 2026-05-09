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
    PYTHONPATH=/app \
    VIRTUAL_ENV=/app/.venv \
    PATH="/app/.venv/bin:${PATH}"

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        curl \
        ca-certificates \
    && pip install --upgrade pip \
    && pip install uv \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps first (cache layer). Explicit venv path keeps the
# install + the runtime PATH in sync; the activate-then-install pattern
# is fragile because each RUN gets its own shell.
COPY pyproject.toml .
RUN uv venv /app/.venv \
    && . /app/.venv/bin/activate \
    && uv pip install -e .

# Build-time sanity check: if any of these imports fail, fail the
# build NOW rather than at container start. Catches venv / PATH /
# missing-dep mistakes early.
RUN python -c "import fastapi, sqlalchemy, sqlmodel, instagrapi, alembic, mcp; print('deps OK')"

# Copy application code
COPY . .

RUN useradd -m appuser && chown -R appuser:appuser /app
USER appuser

RUN mkdir -p /app/logs

EXPOSE 8081

# Default command runs the API. Override with `python -m app.worker` /
# `python -m app.scheduler` for the other process roles. PATH is set
# to `/app/.venv/bin:...` above, so `uvicorn`, `python`, `alembic` etc.
# all resolve directly without `/app/.venv/bin/` prefix.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8081"]
