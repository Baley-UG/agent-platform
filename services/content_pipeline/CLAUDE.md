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
| CP-M1 | Skeleton, data model, storage, project CRUD | ✅ done | abfdac4 |
| CP-M2 | References + intake + analyzer | ✅ done | b7115d3 |
| CP-M3 | Image generation + multi-aspect | ✅ done | _pending commit_ |
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

## CP-M2 deliverables

What landed in CP-M2:
- 3 new tables: `reference_intake_rules`, `scenarios` (with `previous_scenario_json` + `version` for regenerate-with-rollback), `reference_usages` (audit row per (reference, scenario) pair). Migration `0003_cp_m2_scenarios`.
- `app/services/scraper_bridge.py` — read-only SELECT against `ig_scraper.ig_posts` (cross-schema, public schema in same DB) returning a plain dict, no shared SQLModel coupling.
- `content_references` API: `POST /upload` (admin already PUT bytes via presigned URL), `POST /import-from-scraper` (by ig_posts.pk), list/get/patch/archive, `GET /{id}/usage-check`.
- `reference_intake_rules` API: CRUD + `app/services/intake_rules.py` matcher (`min_score`, `min_engagement_rate`, `min_likes`, `min_play_count`, `posted_within_days`, `min_duration_sec`, `max_duration_sec`, `media_types`, `language`, `must_have_caption`, `from_tracked_targets`). Unknown keys ignored (forward-compat). The cron-style scraper subscriber that *invokes* the matcher lands in CP-M2.5.
- `GET /projects/{pid}/inbox/candidates` — shortcut for `references?status=candidate`.
- `app/services/providers/llm/openrouter.py` — concrete `LLMProvider` impl. Vision via `image_url` parts, `response_format=json_object` toggle, latency timing, returns normalized `LLMResponse` with input/output/cached tokens + computed `cost_usd` from `model_routes.cost_per_unit_usd`.
- `app/services/analyzer.py` — `build_user_prompt`, `parse_scenario_json` (tolerant of fenced ```json``` and chatter prefix), `validate_scenario` (shape check), `analyze_reference` (orchestrator returning `(scenario_json, llm_response)`).
- `app/services/scenarios.py` — full state machine + `_ALLOWED_NEXT` map, reuse-policy gate (`block` always denies, `warn` requires `force=true`, `silent` no-op), `_derive_aspect_groups` (PRESETS-aligned: ig_reels/story/tiktok/shorts → `9:16`; ig_feed_45 → `4:5`; ig_feed_11 → `1:1`).
- `app/services/queue.py` — single-purpose RQ enqueue helper. Uses `func_path` strings so the API doesn't import worker code.
- `app/services/generation_calls.py` — ledger writer that also bumps `scenarios.generation_cost_usd` and increments Prometheus counters.
- `app/workers/analyzer.py` — RQ task `app.workers.analyzer.run(scenario_id)`. Resolves the route via `model_router`, picks a concrete provider, runs the async analyzer in `asyncio.run`, records `generation_calls`, transitions to `pending_review` or `failed`.
- `scenarios` API: `POST /` (auto-enqueues analyzer, soft-fails if Redis is down), `POST /{id}/analyze`, `PATCH /{id}` (only in `draft`/`pending_review`), `POST /{id}/approve`, `POST /{id}/regenerate` (snapshots into `previous_scenario_json`, bumps `version`).
- 25 new unit tests across `test_analyzer_parse.py`, `test_intake_matcher.py`, `test_scenario_state_machine.py`, plus expanded `test_smoke.py` table/route assertions. **33/33 green.**

What works now (verified):
- `pytest tests/` → 33/33.
- `alembic upgrade head --sql` emits all 12 tables (9 from CP-M1 + 3 from CP-M2) with valid Postgres DDL.
- All declared routes register on `app.routes`.

Critical contract decisions (don't re-debate):
- **Analyzer output is JSON-strict** — system prompt enforces it; parse layer is tolerant of fenced/chatter wrappers because LLMs occasionally drift.
- **Originality is a system-prompt rule, not a post-filter.** The reference's caption/transcript/metadata is fed to the analyzer; the analyzer is instructed to produce same-genre/mood scenarios without copying frames or quotes. CP-M3 will not pipe original frames into the image generator either.
- **Reuse policy is enforced on `scenarios.create`, NOT on `references.create`.** Importing the same reference twice is fine; spawning a second scenario from it is what triggers the `reuse_policy` gate (block/warn/silent).
- **Aspect-group fan-out is precomputed at scenario-create time** and stored in `scenarios.target_aspect_groups`. CP-M3 reads that array to know how many image-gen jobs to enqueue per scene. Variants (ig_reels vs tiktok) within the same aspect group share scene masters.
- **`scraper_bridge` issues raw SQL**, not SQLModel. The scraper's models live in a different package; coupling our migrations to theirs would create a deploy-order trap. Plain SELECT through the read engine is enough for a one-shot pull on import.
- **`previous_scenario_json` only holds ONE prior version.** Long history isn't a CP-M2 goal — admins who need it can pull from `generation_calls` for cost trace and from S3 for assets. If multi-version rollback turns out to be a real need, add a `scenario_versions` audit table.
- **The analyzer worker uses `asyncio.run` per job.** RQ workers are sync; Python lets us spin up an event loop per task. Acceptable for the analyzer's IO-bound LLM calls; CP-M3 image_gen will follow the same pattern.
- **Provider auth lives in env** (`OPENROUTER_API_KEY`, `FAL_KEY`, `SEEDANCE_API_KEY`, `ELEVENLABS_API_KEY`). The *model selection* lives in `model_routes` — admins change models without redeploys; rotating provider secrets still requires an env-and-restart.
- **Reuse policy `block` denies even with `force=true`.** That's by design — projects opt into block deliberately when they want a hard rule. Loosen to `warn` if you want override.

What's deliberately stubbed:
- Scraper-driven auto-intake (Redis pub/sub subscriber that runs the matcher) — CP-M2.5 polish.
- Reference media auto-mirror to S3 on import-from-scraper — caller can fetch lazily; CP-M3 image generator never needs the original anyway.
- Concrete providers for `fal`, `seedance`, `elevenlabs` — CP-M3..CP-M5.
- `scene_renders`, `render_variants`, `media_assets` writes — CP-M3.

## CP-M3 deliverables

What landed in CP-M3:
- New table `scene_renders` (scenario_id, scene_idx, aspect_ratio, image_asset_id, video_asset_id, status, error). UNIQUE(scenario_id, scene_idx, aspect_ratio). Migration `0004_cp_m3_scene_renders`.
- `app/services/presets.py` — single source of truth for platform specs: `PRESETS` (ig_reels, tiktok, ig_story, ig_feed_45, ig_feed_11, yt_shorts) with safe_zones + max_duration + LUFS, `ASPECT_DIMENSIONS` (9:16 → 1080×1920, 4:5 → 1080×1350, 1:1 → 1080×1080, 16:9 → 1920×1080), `VARIANT_ASPECT_GROUP` reverse map. Lives in code, not DB.
- `app/services/providers/image/base.py` — `ImageProvider` ABC + `ImageResponse` dataclass.
- `app/services/providers/image/fal.py` — `FalImageProvider`. Uses fal.ai's sync `/run` endpoint at `https://fal.run/<model_id>`, maps canonical aspects to fal preset names (`portrait_16_9`, `square`, `portrait_4_3`, `landscape_16_9`) with explicit width/height fallback, fetches the image bytes from fal's CDN before returning so the caller can upload directly to our S3.
- `app/services/media_assets.py` — versioned-chain helpers. `create_initial(...)` for v1, `replace(prior, ...)` for v2+ that bumps `prior.replaced_by_id`, inserts a new row with `version = prior.version + 1` and `previous_version_id = prior.id`. Rollback = swap `replaced_by_id` links; never delete.
- `app/services/scene_renders.py` — `materialize_for_scenario` (idempotent fan-out across scenes × aspect_groups), `mark_image_ready/failed`, `recompute_scenario_status_from_renders` (rolls scene_render statuses up into the parent scenario: any failed → scenario failed, all `image_ready` → scenario `images_ready`).
- `app/workers/image_gen.py` — RQ task `app.workers.image_gen.run(scene_render_id, prompt_override=None)`. Resolves `scene_image` route via model_router, picks dimensions from `presets.ASPECT_DIMENSIONS`, runs the async fal call inside `asyncio.run`, uploads bytes to S3 (`projects/{pid}/scenes/...`), writes versioned `media_assets`, links `scene_render.image_asset_id`, records `generation_calls` (with `image_count=1` and `cost_usd` from route pricing), rolls up scenario status.
- `app/services/scenarios.py` — added `start_image_generation(...)` that gates `approved → generating_images`. `approve` stays focused on the analyzer→approved transition only, so admins can flip target_variants between approve and image-gen.
- API additions on the scenarios router:
  - `POST /scenarios/{id}/start-images` — kicks the whole fan-out: transitions the scenario, materializes `scene_renders` for every (scene, aspect_group), enqueues an image_gen job per pending render.
  - `GET /scenarios/{id}/scene-renders` — list scenes with their per-aspect status (admin grid view).
  - `POST /scenarios/{id}/scenes/{idx}/regenerate-image` — body accepts `{aspect_ratio?, prompt_override?}`. Without `aspect_ratio`, regenerates ALL aspect-group masters for that scene; with one, regenerates just that variant. Each run produces a fresh `media_assets` version; the prior asset stays intact for rollback.
- Tests (15 new, **48/48 green**):
  - `test_presets.py` — every variant has dimensions, 9:16 family shares master, canonical resolutions pinned.
  - `test_fal_size_mapping.py` — fal preset string for canonical sizes, fallback for non-canonical.
  - `test_scene_renders_logic.py` — fan-out math handles missing scenario_json / empty scenes / null aspect_groups.
  - Smoke test extended for the new table + 3 new routes.

What works now (verified):
- `pytest tests/` → 48/48.
- `alembic upgrade head --sql` emits all 13 tables (12 from CP-M1+M2 plus `scene_renders`).
- Routes register: `start-images`, `scene-renders`, `regenerate-image`.

Critical contract decisions (don't re-debate):
- **Approve and image-gen are TWO endpoints, not one.** `POST /approve` only flips status to `approved`. `POST /start-images` is the image-money-spending step. This matches CP-M2's analyzer auto-enqueue pattern (cheap → automatic) vs the more expensive image fan-out (deliberate).
- **Scene masters are per aspect_group, not per variant.** `ig_reels` and `tiktok` both consume the 9:16 master; only `ig_feed_45` would trigger a separate 4:5 image_gen run. Variants diverge at compose-time (CP-M5), not at image-gen.
- **Fan-out width comes from `scenario.target_aspect_groups`** (precomputed at scenario create from `target_variants`). The image_gen worker never re-derives — it just reads `scene_render.aspect_ratio`.
- **fal.ai image bytes get fetched server-side, NOT URL-passed downstream.** fal's CDN URL has a finite TTL; we mirror to our S3 immediately so the asset persists for the entire pipeline lifetime.
- **`media_assets` versioning is per-asset, not per-scenario.** Regenerating scene 0's image at version 3 doesn't bump scene 1's version. The `(version, replaced_by_id)` chain lives independently for every scene_render slot.
- **Regenerate-image requires the scenario to be past `approved`.** Trying to regenerate while in `pending_review` (no scene_renders yet) or already `composing` (the per-aspect masters are downstream-consumed) returns 409.
- **`scene_renders.status` is per-scene; scenario.status is rolled up.** The roll-up rule is conservative: any single failed render fails the whole scenario (admin needs to retry just the failed scene before the pipeline can advance).
- **`asyncio.run` per RQ task** — same pattern as the analyzer worker. Acceptable for IO-bound provider calls; if fal latency tail becomes a problem we'd switch to RQ's async support or queue concurrency in CP-M8.

What's deliberately stubbed:
- Negative prompts / brand_style_suffix injection — the worker has the hook (`_image_prompt`) but doesn't pull from brand_kits yet. CP-M3.5 polish.
- Seed pinning for deterministic regenerates — `provider.generate(seed=...)` is wired but the API doesn't surface it. CP-M8.
- Video generation — CP-M4 picks up where image_gen leaves off.
- Image upscaling / face fixing / model fallback chain — CP-M8 if needed.

## Open questions (still unresolved — flag if a milestone touches one)

1. Default reference intake action (`auto_import` vs `queue_for_review`) — user said "I'll decide".
2. M8 score threshold for auto-intake — needs distribution data from production scraper.
3. Webhook vs polling for Seedance — first version polls; revisit in CP-M4.
4. Soft-delete vs hard-delete for projects — unresolved; soft preferred for now.
5. KMS vs Fernet for `social_accounts.credentials_encrypted` — Fernet OK for dev, KMS later.
