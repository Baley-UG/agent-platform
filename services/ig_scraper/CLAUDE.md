# ig_scraper — Claude context file

> Read this **before** doing any work in `services/ig_scraper/`. It is the
> single-page brief any new Claude session needs to pick up where the last
> one left off. The full design lives in `docs/instagram-scraper-plan.md`
> at the repo root — read that for *why*; this file is for *what's done
> and what's next*.

## What this service is

A FastAPI microservice that scrapes Instagram competitors (per-username
and per-hashtag), stores everything in the **shared main Postgres** under
the `ig_*` table prefix, and exposes the data over REST and MCP. Feeds a
future AI generation pipeline that lives in the main `agent-platform`.

Three processes from one Docker image:
- `app.main` — FastAPI control plane (`/api/v1/...`, `/mcp/...`).
- `app.worker` — asyncio worker that claims jobs from `ig_scrape_jobs`
  via `SELECT ... FOR UPDATE SKIP LOCKED`.
- `app.scheduler` — ticks every minute, turns due `ig_scan_targets` into
  queued jobs.

No Redis. No Celery. No separate database.

## Locked-in design decisions (do NOT re-debate without explicit user OK)

- Shared Postgres with `ig_*` prefix (not a separate DB, not a separate
  schema).
- DB-backed job queue using `FOR UPDATE SKIP LOCKED`.
- Auth = single static `IG_SCRAPER_API_KEY`. Header `X-API-Key` for REST,
  `Authorization: Bearer` for MCP. No JWT, no user table.
- Generic HTTP/SOCKS5 proxy pool. Sticky 1:1 binding between account and
  proxy for the account's lifetime.
- Metadata + IG CDN URLs only in v1. No media downloads.
- instagrapi sessions stored as encrypted blobs on `ig_accounts`; device
  fingerprint generated once, never rotated.
- Anti-detection layer (§ 5.3 of the plan) is mandatory — per-action
  delay tiers, macro-pauses, session caps, active hours, tiered quotas,
  warm-up, adaptive backoff. Not optional, not a "later".

## Two phases

**Phase 1 — Core scraper (M1–M10).** No embeddings, no LLM calls.
Deterministic engagement scoring only.

**Phase 2 — AI substrate (M11–M13).** pgvector embeddings, LLM-derived
features, author style summaries. Layered on top, never modifies Phase 1
schema.

## Milestone status

Update this table at the end of each session. The next session reads it
and starts where you left off.

| ID | Title | Status | Commit |
| - | - | - | - |
| M1 | Foundations | ✅ done | _pending commit_ |
| M2 | Account & proxy management | ⏳ not started | — |
| M3 | Job queue + worker | ⏳ not started | — |
| M4 | Feed scrape flows | ⏳ not started | — |
| M5 | Stories & highlights | ⏳ not started | — |
| M5.5 | MCP read surface | ⏳ not started | — |
| M6 | Hashtag scan + author enrichment | ⏳ not started | — |
| M7 | Scheduler & tracked targets | ⏳ not started | — |
| M8 | Scoring & analytical views | ⏳ not started | — |
| M9 | Webhooks & retention | ⏳ not started | — |
| M10 | Hardening & launch (end of Phase 1) | ⏳ not started | — |
| M11 | pgvector & caption embeddings | ⏳ not started | — |
| M12 | LLM-derived structured features | ⏳ not started | — |
| M13 | Author style + re-ranking (end of Phase 2) | ⏳ not started | — |

Status legend: ⏳ not started · 🔄 in progress · ✅ done · 🚧 blocked.

## Conventions inherited from `agent-platform`

- Python 3.13, dependency manager `uv`, `pyproject.toml` per service.
- `structlog` JSON logs, Prometheus metrics, `slowapi` rate limiting,
  `SQLModel` for ORM, Alembic for migrations.
- Settings via `pydantic-settings` reading the same `.env` files as the
  main app.
- Test runner `pytest`. Linting `ruff` + `black`. Line length 119.

## How to start a fresh session

1. Read this file.
2. `git log --oneline -20` to see what's actually merged.
3. Pick the next ⏳ row in the milestone table above. If one is 🔄, finish
   it first.
4. Read the corresponding section of `docs/instagram-scraper-plan.md`.
5. Implement, test, commit, mark the row ✅ in this file with the commit
   SHA, push.

## M1 deliverables (reference for the next session)

What landed in M1:
- `pyproject.toml` (deps: fastapi, instagrapi, sqlmodel, alembic, psycopg, structlog, prometheus, slowapi, cryptography, pydantic-settings, pillow).
- `Dockerfile` (Python 3.13 slim + uv, port 8081, default CMD = uvicorn).
- `alembic.ini` + `alembic/env.py` + `alembic/versions/0001_initial_phase1.py` — creates all 17 Phase-1 tables in one migration with the indexes specified in the plan.
- `app/core/config.py` — pydantic-settings reading shared `POSTGRES_*` and the full `IG_*` knob set; `PROJECT_NAME`/`VERSION` are `ClassVar` so they don't collide with the main app's env.
- `app/core/logging.py` — structlog (JSON in non-dev, dev-coloured otherwise).
- `app/core/metrics.py` — Prometheus middleware + `/metrics`; pre-declared counters (`ig_jobs_total`, `ig_posts_saved_total`, `ig_account_failures_total`, ...) ready for later milestones.
- `app/services/database.py` — primary engine + optional read replica (`session_scope`, `read_session_scope`, `health_check`).
- `app/models/*` — SQLModel definitions for every Phase-1 table.
- `app/main.py` — FastAPI app with `/health`, `/ready`, `/`, `/metrics`, OpenAPI at `/api/v1/openapi.json`. CORS, structured validation errors, lifespan hooks.
- `app/api/v1/{api,deps,health,jobs,accounts,proxies,targets}.py` — router stubs returning 501 with milestone reference; auth dep (`require_api_key`) wired everywhere it should be.
- `app/worker.py` and `app/scheduler.py` — placeholder long-running processes so docker-compose has something to start.
- Root `docker-compose.yml` — three new services: `ig-scraper-api` (8081), `ig-scraper-worker`, `ig-scraper-scheduler`. All share one image.
- Root `.env.example` — every `IG_*` knob documented with sensible defaults.

What works now (verified):
- `python -c "import app.main"` loads cleanly.
- All 17 tables register on `SQLModel.metadata`.
- All 27 routes wire up correctly.
- `alembic upgrade head --sql` emits valid PostgreSQL DDL.

What's deliberately stubbed (will fail with 501):
- `POST /jobs` and friends → M3.
- Account / proxy CRUD → M2.
- Target CRUD → M7.

Next milestone (M2): account & proxy CRUD, Fernet encryption, login flow that populates `session_blob`, proxy connectivity test endpoint. Read § 5.1–5.2 of the plan first.

## Open questions (still unresolved — flag if a milestone touches one)

1. Multi-tenancy (`tenant_id` on jobs/targets/posts) — not yet decided.
2. Auto-promote vs `pending_review` default for hashtag-discovered users.
3. Story retention policy (currently keep forever).
4. Whether to skip `user_clips` for accounts whose feed already mixes
   reels.
