# ig_scraper

Instagram competitor-content scraper microservice. Part of the
`agent-platform` repo; shares the main Postgres and runs alongside the main
API.

For the full design, milestone status, and conventions read
`./CLAUDE.md` and `../../docs/instagram-scraper-plan.md`.

## Processes

One Docker image, three roles selected by command:

```bash
# API (default)
uvicorn app.main:app --host 0.0.0.0 --port 8081

# Worker (claims jobs, runs scrapes)
python -m app.worker

# Scheduler (turns due tracked targets into queued jobs)
python -m app.scheduler
```

## Local dev

```bash
cd services/ig_scraper
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"

# Run migrations (uses POSTGRES_* env vars from repo root .env)
alembic upgrade head

# Start the API
uvicorn app.main:app --reload --port 8081
```

Health check: `GET http://localhost:8081/health`.
