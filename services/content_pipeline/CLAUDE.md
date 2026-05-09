# content_pipeline — Claude context file

> Read this **before** doing any work in `services/content_pipeline/`. It is the
> single-page brief any new Claude session needs to pick up where the last one
> left off. The full design lives in `services/content_pipeline/PLAN.md` —
> read that for *why*; this file is for *what's done and what's next*.

## What this service is

A FastAPI microservice that uses AI to:
1. Analyze reference content (Instagram-scraped via `ig_scraper`, or manually uploaded)
2. Produce a structured **scenario JSON** (scene-by-scene script with hook/CTA/voiceover)
3. Generate scene images (T2I), scene videos (I2V), voiceover (TTS), and pick music
4. Compose final videos via self-hosted ffmpeg, fanned out per platform format
5. Plan, schedule, and publish to Instagram (Graph API) and TikTok (Content Posting API)

**Multi-tenant** by Project. Each project (X, Y, …) is fully isolated: its own
references, brand kits, social accounts, content plan, asset library, budgets.

The admin panel is a separate UI; this service exposes the API only.

## Locked-in design decisions (do NOT re-debate without explicit user OK)

- **Project is a first-class entity.** Every table has `project_id`. No global state for content.
- **`social_accounts` (publishing) is distinct from `ig_scraper.ig_accounts` (scraping).** Don't conflate them.
- **References are source-agnostic.** `content_references.source_provider` ∈ {instagram, tiktok, manual_upload}.
  When TikTok scraping lands, no schema change here.
- **Central AI model registry.** All provider/model selection goes through `model_routes`.
  Admin edits the table, no redeploy. Provider field is `text` (not enum) — adding a new provider
  is a code change with no migration.
- **Cost ledger.** Every external API call writes a `generation_calls` row. Budget enforcement
  reads from this table.
- **Regenerate at every level.** Scenario, scene image, scene video, voiceover, music, compose,
  full variant. Every regen creates a new `media_assets` version with `replaced_by_id` chain;
  rollback is a swap, not a delete.
- **Multi-aspect via fan-out, not crop.** `scene_renders` keyed by (scenario_id, scene_idx, aspect_ratio).
  9:16 group shares masters across reels/story/tiktok; 1:1 / 4:5 generate fresh.
- **Storage is S3-compatible.** MinIO in dev, Hetzner S3 in prod. Same boto3 code, env-only difference.
- **Job queue is RQ on shared Redis.** Same Redis instance as `ig_scraper`. Separate logical queues:
  `analyzer`, `image_gen`, `video_gen`, `audio_gen`, `media_render`, `publish`, `planner`.
- **ffmpeg worker is a separate container** (`Dockerfile.ffmpeg`). CPU-bound, scales independently.
- **Reuse policy.** `projects.reuse_policy ∈ {block, warn, silent}`. Default `warn`.
- **Weekly plan fill mode.** Default `auto_suggest` (system suggests, admin clicks to confirm).
  Other modes: `manual`, `auto_fill`.

## Two phases

**Phase 1 — CP-M1..CP-M5** — generation pipeline up through final composed videos.
**Phase 2 — CP-M6..CP-M8** — planning, publishing, optimization, multi-platform.

## Milestone status

Update this table at the end of each session. The next session reads it and starts where you
left off.

| ID | Title | Status | Commit |
| - | - | - | - |
| CP-M1 | Skeleton, data model, storage, project CRUD | ✅ done | _pending commit_ |
| CP-M2 | References + intake + analyzer | ⏳ not started | — |
| CP-M3 | Image generation + multi-aspect | ⏳ not started | — |
| CP-M4 | Video generation | ⏳ not started | — |
| CP-M5 | Audio + compose (end of Phase 1) | ⏳ not started | — |
| CP-M6 | Posting strategy + weekly plan + IG publish | ⏳ not started | — |
| CP-M7 | TikTok + auto-generation | ⏳ not started | — |
| CP-M8 | Quality / enhancement | ⏳ not started | — |

Status legend: ⏳ not started · 🔄 in progress · ✅ done · 🚧 blocked.

## Stack & provider choices

| Layer | Choice | Notes |
|---|---|---|
| Framework | FastAPI (mirror `ig_scraper` patterns) | Python 3.13, uv |
| ORM | SQLModel + Alembic | Schema: `content_pipeline` |
| Queue | RQ on shared Redis | Multiple logical queues |
| LLM | OpenRouter via `model_routes` | Vision required for analyzer |
| T2I | fal.ai (Flux) via `model_routes` | |
| I2V | Seedance via `model_routes` | Async polling first, webhook later |
| TTS | ElevenLabs via `model_routes` | `voice_id` per brand_kit |
| Music | Self-hosted `music_tracks` library | Suno integration deferred |
| Compose | Self-hosted ffmpeg worker | `Dockerfile.ffmpeg` |
| Storage | MinIO (dev) / Hetzner S3 (prod) | One boto3 path, env-only swap |
| Publish | IG Graph API (CP-M6), TikTok Content Posting (CP-M7) | |

## Conventions inherited from `agent-platform`

- Python 3.13, dependency manager `uv`, `pyproject.toml` per service.
- `structlog` JSON logs, Prometheus metrics, `slowapi` rate limiting,
  `SQLModel` for ORM, Alembic for migrations.
- Settings via `pydantic-settings` reading the same `.env` files as the main app.
- Test runner `pytest`. Linting `ruff` + `black`. Line length 119.

## How to start a fresh session

1. Read this file.
2. `git log --oneline -20` to see what's actually merged.
3. Pick the next ⏳ row in the milestone table above. If one is 🔄, finish it first.
4. Read the corresponding section of `PLAN.md`.
5. Implement, test, commit, mark the row ✅ in this file with the commit SHA.

## CP-M1 deliverables

What landed in CP-M1:
- `pyproject.toml` (FastAPI, SQLModel, Alembic, RQ, boto3, structlog, prometheus, slowapi, cryptography). `[tool.setuptools.packages.find] include = ["app*"]` because top-level `tests/` and `alembic/` confuse setuptools auto-discovery.
- `Dockerfile` (Python 3.13.2, uv, port 8082) and `Dockerfile.ffmpeg` (same image + `apt-get install ffmpeg`, default cmd consumes only `media_render`).
- `app/core/`: `config.py` (env loading mirrors ig_scraper, exposes `postgres_dsn` and `redis_url`), `logging.py`, `security.py` (Fernet wrapper, refuses placeholder), `s3.py` (boto3 client; one path for MinIO + Hetzner via `S3_USE_PATH_STYLE`), `metrics.py` (Prometheus, pre-declared cp_* counters), `database.py` (primary + read-replica engines, `session_scope`).
- 9 SQLModel tables, all in the `content_pipeline` schema: `projects`, `brand_kits`, `social_accounts`, `content_references`, `templates`, `music_tracks`, `media_assets` (with `version`, `previous_version_id`, `replaced_by_id`), `model_routes`, `generation_calls`.
- Alembic: `env.py` includes the schema in `version_table_schema` and creates it before run; migration `0001_cp_m1` is the full initial DDL with two partial unique indexes for `model_routes` (NULL vs non-NULL `project_id`); `0002_seed_model_routes` seeds 4 global rows for `scenario_analysis`, `scene_image`, `scene_video`, `voiceover_tts`.
- API surface (all gated by `X-API-Key`):
  - `POST/GET/PATCH/DELETE /projects` (soft delete = `status='archived'`)
  - `/projects/{pid}/brand-kits` CRUD with single-default invariant
  - `/projects/{pid}/social-accounts` CRUD; credentials encrypted on write, never returned (read shape exposes `has_credentials: bool`)
  - `/projects/{pid}/templates`, `/projects/{pid}/music-tracks` CRUD
  - `/projects/{pid}/assets/upload-url` — presigned PUT URL (admin uploads bytes direct to S3)
  - `/projects/{pid}/model-routes` CRUD (project-scoped) + `/global/model-routes` CRUD (global defaults)
- `app/services/model_router.py` — `resolve(task_key, project_id)` and `resolve_chain(...)` with project → global precedence; CRUD shared with REST layer.
- `app/services/providers/llm/base.py` — `LLMProvider` ABC, `LLMResponse`, `VisionInput`. Concrete impls land in CP-M2.
- `app/main.py` — FastAPI lifespan, CORS, validation handler, `/health`, `/ready`, `/metrics`.
- `app/worker.py` — RQ worker entry; `--queues` flag selects which queues to consume. Generic worker default = `analyzer image_gen video_gen audio_gen publish planner`. Render container overrides cmd to `--queues media_render`.
- `app/scheduler.py` — async tick loop skeleton with SIGINT/SIGTERM handling. Real cron jobs land in CP-M6+.
- `docker-compose.yml`: added `redis`, `content-pipeline-api`, `content-pipeline-worker`, `content-pipeline-render`, `content-pipeline-scheduler`. Host port via `CP_HOST_PORT` (default 8082).
- `docker-compose.override.yml`: dev hot-reload mounts + `minio` + `minio-init` (auto-creates the bucket). Console at <http://localhost:9001>.
- `.env.example`: full `CP_*`, `REDIS_*`, `S3_*` block with both dev (MinIO) and prod (Hetzner) examples.
- `tests/test_smoke.py` — 8 tests: imports, table-registration count, route mounting, security round-trip + placeholder rejection, S3 key namespacing, presigned URL shape, pydantic schema parse. **All green** under `pytest tests/`.

What works now (verified):
- `python -c "import app.main"` clean.
- `pytest tests/` → 8/8 green.
- `alembic upgrade head --sql` emits valid Postgres DDL for both migrations.
- All declared routes register on `app.routes`.

Critical contract decisions (don't re-debate):
- **Schema isolation is `content_pipeline` Postgres schema**, not a separate database. Same DSN as `ig_scraper`. Cross-DB joins to `ig_scraper` tables stay possible (CP-M2 uses this for `import-from-scraper`).
- **Redis DB index is `1`** — distinct from any future ig_scraper Redis usage. ig_scraper itself does not use Redis today.
- **Presigned upload only**, never proxy bytes through the API. Admin → presign → direct PUT to S3 → patch the resource with the returned `s3_key`.
- **Soft delete for projects** (`status='archived'`); FK cascades remain, but `get_project` dependency 404s on archived rows so they vanish from the API.
- **`social_accounts.credentials_encrypted` is bytes (LargeBinary)**, never plaintext. Read shape never exposes them; the publisher worker uses `get_decrypted_credentials()` only on write.
- **`model_routes` uniqueness is via two partial indexes** (one for `project_id IS NULL`, one for project-scoped). NULLs in standard unique constraints would let duplicate global rows in.
- **`media_assets` versioning chain**: `(version, previous_version_id, replaced_by_id)`. Active asset = latest version with `replaced_by_id IS NULL`. Rollback swaps the chain, never deletes.
- **`content_references.source_external_id` is nullable** (manual_upload rows have no external id) — uniqueness enforced via partial unique index `WHERE source_external_id IS NOT NULL`.
- **RQ 2.x** removed `Connection` context manager; we pass `connection=` directly to `Queue` and `Worker`. Easy to miss when copying patterns from older RQ docs.

What's deliberately stubbed:
- Provider concrete impls (OpenRouter, fal.ai, Seedance, ElevenLabs) — CP-M2 onward.
- All scenario / scene / variant tables — CP-M2..CP-M5.
- Worker job dispatch — CP-M2 onward.
- Real scheduler cron jobs — CP-M6.
- IG / TikTok publishers — CP-M6 / CP-M7.

## Open questions (still unresolved — flag if a milestone touches one)

1. Default reference intake action (`auto_import` vs `queue_for_review`) — user said "I'll decide".
2. M8 score threshold for auto-intake — needs distribution data from production scraper.
3. Webhook vs polling for Seedance — first version polls; revisit in CP-M4.
4. Soft-delete vs hard-delete for projects — unresolved; soft preferred for now.
5. KMS vs Fernet for `social_accounts.credentials_encrypted` — Fernet OK for dev, KMS later.
