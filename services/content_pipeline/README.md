# content_pipeline

AI content production pipeline. Turns scraped or uploaded reference content into similar
Instagram / TikTok posts, plans them on a calendar, and publishes via official APIs.

See [`PLAN.md`](./PLAN.md) for the full design and [`CLAUDE.md`](./CLAUDE.md) for the
session-to-session status brief.

## Local dev

```bash
cd services/content_pipeline
uv venv .venv && source .venv/bin/activate
uv pip install -e '.[dev]'

# DB migrations (against the docker-compose Postgres):
alembic upgrade head

# Run pieces:
uvicorn app.main:app --reload --port 8082
python -m app.worker
python -m app.scheduler
```

MinIO is started by `docker-compose.override.yml`; the bucket is auto-created.
The console lives at <http://localhost:9001> (`minioadmin` / `minioadmin`).
