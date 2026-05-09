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

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        curl \
        ca-certificates \
    && pip install --upgrade pip \
    && pip install uv \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
RUN uv venv /app/.venv \
    && . /app/.venv/bin/activate \
    && uv pip install -e .

RUN python -c "import fastapi, sqlalchemy, sqlmodel, alembic, boto3, rq; print('deps OK')"

COPY . .

RUN useradd -m appuser && chown -R appuser:appuser /app
USER appuser

RUN mkdir -p /app/logs

EXPOSE 8082

# Default: API. Override with `python -m app.worker` / `python -m app.scheduler`.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8082"]
