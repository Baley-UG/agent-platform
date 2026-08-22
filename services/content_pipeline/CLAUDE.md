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
| CP-M3 | Image generation + multi-aspect | ✅ done | aeb3750 |
| CP-M4 | Video generation | ✅ done | d235023 |
| CP-M5 | Audio + compose (end of Phase 1) | ✅ done | 04895ec |
| CP-M6 | Posting strategy + weekly plan + IG publish | ✅ done | 3423fe5 |
| CP-M7 | TikTok + auto-generation | ✅ done | 1d9d820 |
| CP-M6.5 + CP-M8 (selective) | Captions, aggregate progress, dedup, curator | ✅ done | e11aa13 |
| CP-M8.5 | Auth, users, project memberships (in content_pipeline) | ❌ superseded by CP-M9 | 131e23b |
| CP-M9 | Auth centralized in main `app/`; gateway proxy; CP auth removed | ✅ done | 4b18615 |
| CP-R1 | `repurpose` mode — cut real segments from the source reel | ⛔ superseded by CP-M10 | — |
| CP-M10 | Remake vertical — replaces the scenario pipeline entirely. remakes/remake_shots/remake_steps + idempotent reconciler + 3 queues (remake_ffmpeg/ai/analysis) + FalQueueClient. copy/erase/restyle/reframe techniques, 2 human gates, source-audio kept by default. Migration 0012 drops scenarios/scene_renders/render_variants/reference_usages/auto_generation. | ✅ done | b36bf90 |
| CP-M8 (rest) | pgvector embeddings, webhooks, outpaint, Suno, etc. | ⏳ deferred | — |

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

## CP-M4 deliverables

What landed in CP-M4:
- `app/services/providers/video/base.py` — `VideoProvider` ABC + `VideoResponse` dataclass.
- `app/services/providers/video/seedance_fal.py` — Seedance image-to-video via fal.ai's async queue at `https://queue.fal.run/<model_id>`. Submit → poll status_url with 5s→30s backoff (capped at `CP_VIDEO_GEN_TIMEOUT_SECONDS=600`) → fetch response_url → download video bytes from fal CDN. Cost computed from `cost_unit='video_second'` × scene duration. Falls back from `SEEDANCE_API_KEY` to `FAL_KEY` so the user can keep one key today and split later.
- Migration `0005_cp_m4_seedance_route` updates the global `scene_video` row's `model_id` from the placeholder `seedance-v1-pro-i2v` to fal.ai's actual route name `fal-ai/bytedance/seedance/v1/pro/image-to-video`. Idempotent + reversible.
- `app/services/scene_renders.py` — added `mark_generating_video`, `mark_video_ready`, `claim_for_video_regenerate` (refuses if no image yet), `renders_with_video_pending` (the `image_ready` queryset that the start-videos endpoint feeds to the worker).
- `app/services/scenarios.py` — added `start_video_generation(...)` gating `images_ready → generating_videos`.
- `app/workers/video_gen.py` — RQ task `app.workers.video_gen.run(scene_render_id, motion_override=None)`. Reads scene_render.image_asset_id, presigns a short-lived S3 GET URL for the provider, runs async Seedance call inside `asyncio.run`, uploads bytes to `projects/{pid}/scenes/<scenario_id>-scene-<idx>-<aspect>.mp4`, writes versioned `media_assets` (initial or `replace(prior, ...)` chain), links `scene_render.video_asset_id`, records `generation_calls` with `video_seconds` + `cost_usd`, rolls up scenario status. Helper functions are pure: `_motion_prompt` (falls back from motion_prompt → image_prompt + generic motion → completely-empty default), `_scene_duration` (rejects 0 / negative).
- `app/schemas/scene_renders.py` — added `RegenerateVideoRequest`.
- API additions on the scenarios router:
  - `POST /scenarios/{id}/start-videos` — transitions state, enqueues a video_gen job for every render that's `image_ready`.
  - `POST /scenarios/{id}/scenes/{idx}/regenerate-video` — body `{aspect_ratio?, motion_override?}`. Without aspect_ratio regenerates ALL aspect-group masters for that scene; with one, just that variant.
- Tests (8 new, **56/56 green**):
  - `test_video_gen_helpers.py` — scene lookup, motion fallback chain, duration coercion (positive only).
  - Smoke test extended for the 2 new routes.

What works now (verified):
- `pytest tests/` → 56/56.
- `alembic upgrade head --sql` chain clean: 0001 → 0002 → 0003 → 0004 → 0005, including the model_id update for the seedance route.
- Routes register: `start-videos`, `scenes/{idx}/regenerate-video`.

Critical contract decisions (don't re-debate):
- **Seedance comes via fal.ai's queue, not direct Volcano Engine.** Same auth as image_gen (FAL_KEY). If we later move to a direct ByteDance integration, drop a `seedance_volc.py` next to `seedance_fal.py` and update the worker's `_build_provider` mapping. The `model_routes.provider='seedance'` value stays — admins don't care which transport we use under the hood.
- **Provider-side polling, not webhooks.** Seedance jobs typically finish in 30-90s; a sync polling loop inside the RQ worker is simpler than persisting webhook callbacks. Webhook support deferred to CP-M8 if RQ worker latency becomes a problem.
- **Image is presigned, NOT POSTed.** We give the provider a short-lived GET URL for the source image rather than uploading bytes. Cuts request size 100×.
- **`asyncio.run` per RQ task** stays (matches analyzer + image_gen). Long polling is fine inside it because the worker process is single-job-at-a-time.
- **`scene.duration` rounds UP for Seedance.** A 5.4s scene asks for 6s of motion so the compose stage has slack rather than gaps. ffmpeg compose will re-cut to the exact scene duration in CP-M5.
- **Rollup rule unchanged**: any failed render fails the whole scenario. A single Seedance hiccup blocks the pipeline until admin retries that scene. Conservative but correct — if scene N fails, scenes N+1..N-1's videos are still useful for the eventual retry.

What's deliberately stubbed:
- Direct Volcano Engine ByteDance client — wait until fal.ai's markup is documented to be material. The interface is one file when needed.
- Seed pinning for deterministic regenerates — `provider.generate(seed=...)` is wired but the API doesn't surface it. CP-M8.
- Webhook-driven completion — CP-M8 if needed.
- Audio + compose — CP-M5 picks up where video_gen leaves off.

## CP-M5 deliverables (Phase 1 — closed)

What landed in CP-M5:
- New table `render_variants` (scenario_id, preset_key) with `final_asset_id`, `thumbnail_asset_id`, `render_recipe` JSONB, `duration_sec`, `file_size_bytes`, `error`, `approved_at`. UNIQUE(scenario_id, preset_key). Migration `0006_cp_m5_render_variants` also adds `scenarios.voiceover_asset_id` (FK media_assets) and `scenarios.music_track_id` (FK music_tracks) with `ON DELETE SET NULL`.
- `app/services/providers/tts/base.py` — `TTSProvider` ABC + `TTSResponse`.
- `app/services/providers/tts/elevenlabs.py` — `ElevenLabsProvider`. POST `/v1/text-to-speech/{voice_id}` with `{text, model_id, voice_settings, language_code?}`, returns binary mp3. Cost computed from `cost_per_unit_usd × len(text)` (ElevenLabs charges per character; we tag the unit as `input_token` by convention).
- `app/services/audio.py` — `build_voiceover_script` concatenates `scene.voiceover` strings with periods; empty scenes contribute a `...` pause marker. `select_music_for_scenario` picks a music_track from the project library by mood overlap, falling back to newest. Returns `None` for empty libraries.
- `app/services/scenarios.py` extensions — added `start_audio_generation`, `mark_audio_ready`, `start_compose`, `mark_final_pending_review`, `approve_final`. Updated `_ALLOWED_NEXT` to permit `videos_ready → generating_videos` (scene-video regenerate), `audio_ready → generating_audio` (voiceover regenerate), and `approved_final → composing` (full recompose after approval).
- `app/services/render_variants.py` — `materialize_for_scenario` (idempotent fan-out across `scenario.target_variants`), `mark_composing/ready/failed/approve`, `recompute_scenario_status_from_variants` (any failed → scenario `failed`; all `ready`/`approved` → scenario `final_pending_review`), `claim_for_recompose`.
- `app/workers/audio_gen.py` — RQ task `app.workers.audio_gen.run(scenario_id, voice_id_override?, text_override?)`. Resolves voice_id from brand_kit (project default → any project kit), runs ElevenLabs in `asyncio.run`, uploads to S3 (`projects/{pid}/audio/`), writes versioned `media_assets` of type `voiceover`, links `scenario.voiceover_asset_id`, **auto-picks a music track** if none chosen yet, transitions scenario to `audio_ready`, records `generation_calls`.
- `app/services/renderer.py` — pure `build_compose_command(...)` returns ffmpeg argv as a list; `compose_variant(...)` orchestrates download → ffmpeg → upload using a tempdir. Baseline pipeline: concat-demuxer for scene videos, scale+pad to preset dimensions, voiceover + music mixed via `amix`, `loudnorm` to LUFS target, h264+aac+faststart. `FFmpegError` surfaces missing-binary cleanly. `write_concat_list` produces a properly-escaped concat file list.
- `app/workers/render.py` — RQ task `app.workers.render.run(variant_id)`. Gathers scene video keys for the variant's aspect_group from `scene_renders`, picks voiceover + music keys from the scenario, calls `compose_variant`, writes versioned `media_assets` of type `final_video`, links `render_variant.final_asset_id`, records `generation_calls` with `provider='self_ffmpeg'` and `cost_usd=0`, rolls up scenario status.
- API additions on the scenarios router:
  - `POST /scenarios/{id}/start-audio` — videos_ready → generating_audio + enqueue audio_gen.
  - `POST /scenarios/{id}/regenerate-voiceover` — same gate, accepts `{voice_id_override?, text_override?}`. New media_assets version.
  - `POST /scenarios/{id}/reselect-music` — sets `scenario.music_track_id` (admin-supplied or auto-picked), no TTS re-run.
  - `POST /scenarios/{id}/start-compose` — audio_ready → composing, materializes render_variants × `target_variants`, enqueues media_render for each.
  - `GET /scenarios/{id}/render-variants` — admin grid view.
  - `POST /scenarios/{id}/render-variants/{variant_id}/recompose` — re-run ffmpeg only (no LLM/fal/Seedance/TTS spend).
  - `POST /scenarios/{id}/render-variants/{variant_id}/approve` — flip variant to approved.
  - `POST /scenarios/{id}/approve-final` — final_pending_review → approved_final (scenario-level final approval).
- Tests (16 new, **72/72 green**):
  - `test_audio_helpers.py` — script concatenation w/ pause markers, music selection by mood overlap, fallback to newest, empty library, string-form mood block.
  - `test_renderer_argv.py` — ffmpeg argv shape (-y, -hide_banner, concat demuxer, voiceover/music inputs, `amix` only when both present, `loudnorm` always, scale dims per preset, libx264 + aac + faststart, concat list writer escaping).
  - Smoke test extended for the new table + 6 new routes.

What works now (verified):
- `pytest tests/` → 72/72.
- `alembic upgrade head --sql` chain clean: 0001 → … → 0006. 14 tables emit.
- All declared routes register on `app.routes`.

Critical contract decisions (don't re-debate):
- **Voiceover is ONE TTS call per scenario**, not per scene. Simpler S3 layout, simpler regenerate semantics. Scene-aligned timing markers + per-scene re-record land in CP-M5.5 if scene-level voiceover replay turns out to matter.
- **Music selection runs inside the audio_gen worker**, not as a separate stage. Auto-pick is best-effort (mood overlap → newest → none). Admin can swap at any time via `/reselect-music` without re-running TTS.
- **Compose has `provider='self_ffmpeg'` in `generation_calls` with `cost_usd=0`.** We still record latency + status so the cost-summary endpoint shows compose as a free row in the trace.
- **`render_variants.render_recipe` is a snapshot of the ffmpeg decisions**, not a live config. Recompose with the same recipe should produce ~identical output (ffmpeg is mostly deterministic; libx264 isn't fully deterministic, but byte-equal isn't a goal).
- **Renderer downloads inputs to a tempdir** rather than streaming from S3. ffmpeg's S3 support varies by build; local files are universally portable. Tempdir cleanup is `try/finally`.
- **`build_compose_command` is pure**, separated from `compose_variant`. Tests assert argv shape without invoking ffmpeg — keeps CI fast and binary-independent.
- **State machine permits `videos_ready → generating_videos`**, `audio_ready → generating_audio`, and `approved_final → composing` so admin regenerates don't require fail-then-restart. The existing fail-recovery path (any state → failed → draft/analyzing) is unchanged.
- **Per-scene voiceover timing alignment is deferred.** Today the voiceover plays straight through the concatenated video; if the script is shorter than the video we pad with silence (`-shortest` ensures we don't extend video). If longer, ffmpeg trims.

What's deliberately stubbed (CP-M5.5 / CP-M8 polish):
- Per-scene xfade transitions — concat demuxer is a hard cut today.
- Sidechain ducking under voiceover — `amix` mixes at fixed volumes for now.
- On-screen text overlays at preset safe zones — not yet emitted.
- Outro template insertion — `ComposeInputs.outro_video_key` exists but the arg builder ignores it.
- Multi-region compositions (split / PiP) — captured in PLAN § 11.2.
- Asset injection (app screenshots / phone mockups) — captured in PLAN § 11.3.
- Thumbnail asset generation — `render_variants.thumbnail_asset_id` exists but the worker doesn't populate it; the admin panel uses the first scene's image as a fallback.

## Phase 1 — closed.

The pipeline can now take a reference (scraped or uploaded) all the way to a final composed video per platform. Next:
- CP-M6: posting_strategy, weekly plans, plan_slots, IG Graph API publisher.
- CP-M7: TikTok publisher + scraper bridge for TT, auto_generation_rules.
- CP-M8: quality / dedup / curator / outpaint / Suno / etc.

## CP-M5+ admin display gaps (post-M5 polish)

Critical endpoints the admin panel needs to render the pipeline state. Same milestone as CP-M5 (no new tables, no migration), filed separately because they were called out only when the admin UI scope came up.

What landed:
- `GET /projects/{pid}/media-assets/{asset_id}` — full row (type, s3_key, size, dimensions, version, replaced_by_id, metadata).
- `GET /projects/{pid}/media-assets/{asset_id}/preview-url?ttl=N` — short-lived **presigned GET** so the browser can `<img>`/`<video>` against private MinIO/Hetzner buckets. Without this, the panel had no way to show anything.
- `GET /projects/{pid}/media-assets/{asset_id}/history` — full version chain (oldest → newest). Caller can pass any version id; `walk_chain` resolves the root via `previous_version_id` then walks forward via `replaced_by_id`. Cycle-guarded.
- `GET /projects/{pid}/cost-summary?from=&to=` — aggregates `generation_calls` over a window (default last 30 days). Returns `total_cost_usd`, success/failed counts, breakdown by `task_key`, breakdown by `(provider, model_id)`, and `weekly_budget_remaining_usd` when `projects.weekly_budget_cap_usd` is set.
- `GET /projects/{pid}/scenarios/{sid}/generation-calls` — drill-down list of every external API call we made for that scenario. Project scope enforced defensively at filter time.
- `app/services/media_assets.py` — added `walk_chain(...)` and `active_version(...)`.
- `app/services/cost.py` — `project_summary(...)`, `list_calls_for_scenario(...)`.
- 8 new tests in `test_media_assets_chain.py` covering walk_chain semantics (root / middle / active / single / missing / cycle-bounded / active resolver).

Tests: 80/80 green.

Critical contract decisions:
- **Preview URL TTL is admin-overridable per call** (`?ttl=N` between 60 and 86400 seconds), default from `S3_PRESIGNED_URL_TTL_SECONDS`. Lets the panel hand a short URL to a thumbnail and a longer URL to a full-screen preview without changing global config.
- **Asset history is computed client-side from chain links**, not a separate audit table. The (`previous_version_id`, `replaced_by_id`) chain is the source of truth — a separate audit would drift.
- **Cost summary's "weekly remaining" uses ISO week (Monday 00:00 UTC)**, matching the same anchor PLAN § 5 will use for scheduler cron jobs.
- **Generation_calls list is per-scenario only** for now (no project-wide listing endpoint). Project-wide drill-down is the cost-summary's job; if admins want raw rows project-wide, CP-M8 can add a paginated listing.

## CP-M6 deliverables

What landed in CP-M6:
- 4 new tables in migration `0007_cp_m6_planning_publish`:
  - `posting_strategy` (one row per project; lazy-created on first GET) — `timezone`, `weekly_quota` JSONB, `preferred_slots` JSONB, `min_gap_minutes`, `blackout`, `fill_strategy` (manual/auto_suggest/auto_fill, default `auto_suggest`), `auto_generate_if_empty` (off/suggest/auto, default `suggest`), `approval_required_before_publish`, `weekly_budget_cap_usd`.
  - `weekly_plans` — one row per (project, Monday-of-week). UNIQUE(project_id, week_start_date). Status `draft|approved|active|archived`.
  - `plan_slots` — one row per scheduled post. Holds `scheduled_at` (UTC), `social_account_id`, `content_type`, `variant_preset`, `source_kind`, `variant_id`, `reference_id`, `status`, `suggested_variant_ids[]`, `publish_job_id`, `last_error`. Partial index on `(scheduled_at, status) WHERE status IN ('ready','scheduled')` for the publisher poller.
  - `publish_jobs` — one row per publish attempt. Holds `provider_container_id`, `provider_media_id`, `attempts`, `last_error`, `response` JSONB.
- `app/services/posting_strategy.py` — `get_or_create` (lazy seed on first read with sensible IST defaults: 5 reels/14 stories/3 feed/7 tiktok per week, default `preferred_slots`, `auto_suggest` fill).
- `app/services/planner.py` — pure-logic core:
  - `parse_slot_expression("daily 19:00" | "Mon 12:00" | "Mon,Wed,Fri 19:00" | "weekdays 09:00" | "weekends 11:00")`. Unknown forms return `[]` so a single typo can't fail a whole week's generation.
  - `expand_preferred_slots(strategy, week_start)` returns `[(scheduled_at_utc, preset, content_type)]` sorted by time, capped per-preset by `weekly_quota`.
  - `is_in_blackout(...)` and `respect_min_gap(...)` for filter constraints.
  - `stock_for_preset(...)` / `stock_for_project(...)` — view of `render_variants WHERE status='approved' AND id NOT IN (active plan_slots)`.
  - `suggest_for_slot(...)` (top-3 stock candidates → `plan_slots.suggested_variant_ids`).
  - `auto_fill_slot(...)` (FIFO over approved variants).
  - `monday_of(date)` ISO-week anchor.
- `app/services/weekly_plans.py` — orchestrator: `generate(project, week_start, fill=True)` is idempotent (re-running for the same week reuses the row and only inserts missing slots). `fill_empty_slots(plan, strategy)` applies the fill_strategy. Plan-slot CRUD + `due_slots(now)` queryset for the publisher poller.
- `app/services/providers/social/instagram.py` — `InstagramPublisher` for the Graph API two-step flow (create container → poll until FINISHED → media_publish). 5s→30s polling backoff, 600s hard deadline. `variant_to_ig_media_type(...)` maps `ig_reels→REELS`, `ig_story→STORIES`, `ig_feed_*→VIDEO`. Refuses empty `access_token` / `ig_user_id`.
- `app/services/publishing.py` — publish_jobs CRUD + state transitions (`create_pending`, `mark_uploading/processing/published/failed`). Decrypts `social_accounts.credentials_encrypted` only when handed off to the worker.
- `app/workers/publish.py` — RQ task `app.workers.publish.run(plan_slot_id, force_now=False)`. Loads slot + variant + final_asset, picks `s3.public_url` if the bucket allows it, else a 24h presigned GET (so Meta can fetch through processing). Idempotent: if the slot already has a `published` job, it returns immediately.
- `app/scheduler.py` — promoted to five concurrent loops:
  - `_heartbeat_loop` (existing).
  - `_publisher_poller_loop` (every 60s) — calls `due_slots`, enqueues each.
  - `_plan_filler_loop` (hourly) — re-runs fill_strategy across draft/approved plans.
  - `_weekly_autogen_loop` (Sundays 18:00 UTC) — for every active project, ensure next week's weekly_plan exists. In-memory date marker prevents double-runs across ticks.
  - `_stale_alerter_loop` (hourly) — logs warnings for scenarios stuck in `generating_*` / `composing` / `analyzing` for >2h.
- API additions:
  - `GET /projects/{pid}/posting-strategy` (lazy create) and `PUT /...` for partial update.
  - `POST /projects/{pid}/weekly-plans/generate` (body: `{week_start, fill}`) — idempotent.
  - `GET /projects/{pid}/weekly-plans` and `GET /{plan_id}` and `GET /{plan_id}/slots`.
  - `POST /weekly-plans/{plan_id}/approve` / `/refill`.
  - `POST /projects/{pid}/plan-slots` create, `PATCH /{slot_id}` (drag-drop), `POST /{slot_id}/assign-variant`, `POST /{slot_id}/skip`, `DELETE`.
  - `GET /projects/{pid}/stock?preset=...` — approved+unpinned variants.
  - `GET /projects/{pid}/calendar?from=&to=` — slots in window.
  - `POST /projects/{pid}/plan-slots/{slot_id}/publish-now` — bypass scheduler, enqueue immediately.
  - `GET /projects/{pid}/plan-slots/{slot_id}/publish-jobs` — attempt history.
- Tests (30 new, **110/110 green**):
  - `test_planner.py` — slot expression parsing (daily / weekdays / csv / unknown days / bad time), expansion (quota cap / zero-quota skip / Istanbul→UTC offset / unknown timezone fallback / sorting / invalid-expression skipping), blackout (specific day / daily / outside window / malformed), min_gap (zero / blocking / passing), `monday_of` (Monday/Wed/Sun).
  - `test_ig_publisher.py` — empty-token rejection, valid construction, variant→media_type mapping (reels / stories / feed fallback / unknown).
  - Smoke test extended for the 4 new tables + 7 new route prefixes.

What works now (verified end-to-end against the running stack):
- `pytest tests/` → 110/110.
- Migration 0007 applied cleanly via `alembic upgrade head` against the docker-compose Postgres.
- `GET /projects/{pid}/posting-strategy` lazy-creates the row with IST defaults.
- `POST /weekly-plans/generate` for week 2026-05-11 produces 28 slots with correct UTC times (IST 19:00 → UTC 16:00 for ig_reels, IST 20:00 → UTC 17:00 for tiktok, IST 09:00 → UTC 06:00 for stories, IST 13:00 → UTC 10:00).

Critical contract decisions (don't re-debate):
- **`posting_strategy` is one-row-per-project, lazy-created on first GET.** Avoids a separate "create strategy first" UX step. Default IST timezone matches the user's market.
- **Times stored UTC, expressed in `posting_strategy.timezone`.** Slot expressions are local; `expand_preferred_slots` converts to UTC at materialization time. `from_/to` calendar queries take UTC datetimes.
- **Skeleton generation is idempotent** — re-running `generate(week_start)` reuses the existing weekly_plan and only inserts missing slot tuples. New `preferred_slots` entries pop in on the next run.
- **Variant fan-out uses `weekly_quota[preset]` as a hard cap.** Extra `preferred_slots` entries beyond the quota are dropped (declared-order FIFO). Admins who want more posts adjust the quota, not the slots list.
- **`fill_strategy=auto_suggest` is the default** (PLAN § 5). Skeleton + 2-3 stock candidates per slot. Admin clicks one. `auto_fill` and `manual` available per project.
- **Publisher poller picks slots only when** `scheduled_at <= now() AND status='ready' AND variant_id IS NOT NULL AND social_account_id IS NOT NULL`. Slots without bound variant/account silently wait — admin sees them in the calendar.
- **IG public_url contract**: the renderer should land final_video on a publicly-fetchable URL (Hetzner public bucket). Dev (MinIO localhost) can't be reached by Meta — this is a prod-only flow today.
- **Scheduler in-memory date markers** for weekly_autogen + stale_alerter are sufficient because the scheduler is single-replica. Multi-replica scheduler would need a DB lock — but PLAN explicitly limits the scheduler to one replica.
- **`publish_jobs` is append-only-ish.** A failed job stays at `status='failed'`; a retry creates a new row (so the attempt history grows). The slot's `publish_job_id` always points at the LATEST attempt.
- **Auth on social_accounts** — `credentials_encrypted` JSON expected to contain `{"access_token": "...", "ig_user_id": "..."}`. Other fields are ignored. CP-M7 will add `tt_open_id` / `tt_access_token` etc.

What's deliberately stubbed (CP-M6.5 / CP-M8 polish):
- Captions on plan_slots — the IG publisher passes an empty caption today. Add `plan_slots.caption_override` + `scenarios.default_caption` in CP-M6.5.
- Auto-generation rules (CP-M2 schema deferred to CP-M7) — when stock runs out, no scenario is auto-spawned yet; only the suggest path runs.
- Per-platform constraints validator — e.g. "tiktok max_duration ≤ 600s" is in `presets.py` but not enforced at slot-create time.
- Hashtag generator — admin enters them manually for now via patch on plan_slots (CP-M6.5).
- Multi-image carousel feed posts — single-asset only.
- Webhook receiver from Meta for publish status updates — we poll inside the worker today.
- TikTok publisher (CP-M7).

## CP-M7 deliverables

What landed in CP-M7:
- 1 new table: `auto_generation_rules` (project-scoped) — `name`, `enabled`, `pick_strategy ∈ {highest_score, newest, diverse}`, `daily_quota`, `target_variants[]`, `quality_tier`, `budget_cap_usd`, `last_run_at`. Migration `0008_cp_m7_auto_generation`.
- `app/services/providers/social/tiktok.py` — `TikTokPublisher` for the v2 Content Posting API. PULL_FROM_URL flow: `/post/publish/video/init/` → poll `/post/publish/status/fetch/` until `PUBLISH_COMPLETE` or `FAILED` (4s→30s backoff, 600s deadline). Returns `{publish_id, publicaly_available_post_id?, ...}`. Auth: `{access_token, open_id?}` in `social_accounts.credentials_encrypted`.
- `app/workers/publish.py` — `_build_publisher` now switches on provider: `instagram` | `tiktok`. Publish call argument shape differs (IG: `caption + media_type`; TT: `title`). The media_id extractor falls back from `id` → `publicaly_available_post_id` → `publish_id`.
- `app/services/budget.py` — `week_start_utc(now)` (ISO Monday 00:00 UTC), `day_start_utc(now)`, `weekly_spent`, `daily_spent`, `has_weekly_budget_remaining(project, headroom_usd)`, `has_rule_budget_remaining(project_id, rule_cap, headroom_usd)`. Used by the auto-gen loop and exposable to admin via `/cost-summary`.
- `app/services/auto_generation.py` — `run_rule(rule, project)` runs all checks (enabled, daily_quota via `created_by="auto_gen:{rule_id}"` count, per-rule weekly budget, project weekly budget, candidate availability) then calls `scenarios_svc.create(...)` and bumps `last_run_at`. Pick strategies: `highest_score` (orders by `metadata.score`), `newest`, `diverse` (round-robin authors). `run_all_due()` walks all enabled rules across active projects.
- `app/scheduler.py` — added `_auto_gen_loop` (hourly) running `run_all_due`. Now 6 concurrent loops in the scheduler.
- API additions: `POST/GET/PATCH/DELETE /projects/{pid}/auto-generation-rules[/{rule_id}]`, `POST /{rule_id}/run-now` (bypass cron, returns spawned scenario id or a reason string when nothing eligible).
- Tests (9 new, **119/119 green**):
  - `test_tiktok_publisher.py` — empty-token rejection, valid construction, header shape with bearer auth.
  - `test_budget.py` — `week_start_utc` Monday anchor, `day_start_utc` normalization, no-cap pass-through.
  - Smoke covers the new table + the new auto-generation route prefix.

What works now (verified end-to-end against the running stack):
- `pytest tests/` → 119/119.
- Migration 0008 applied cleanly via `alembic upgrade head`.
- `POST /auto-generation-rules` creates a rule; `GET` lists it.
- TikTok publisher constructs correctly; will run in prod once a TT social_account is provisioned with `{access_token, open_id}` credentials.

Critical contract decisions (don't re-debate):
- **TikTok publisher uses PULL_FROM_URL**, matching IG. `FILE_UPLOAD` (chunked) is implementable later but adds complexity; PULL_FROM_URL also keeps the public-URL contract identical (Hetzner public bucket OR 24h presigned GET).
- **Auto-gen scenarios are tagged via `scenario.created_by="auto_gen:{rule_id}"`**. Daily quota counter reads this tag, not a separate audit table — keeps the schema tight. Manual scenarios use `created_by="api"` so they don't count.
- **Auto-gen NEVER bypasses the reuse policy.** Rule fires `force=False`; if a project has `reuse_policy='warn'` and the candidate was used before, the create raises 409 and auto-gen logs a warning + skips. Admin's call to ack via the panel.
- **Budget caps are weekly, not monthly.** Anchored to ISO Monday 00:00 UTC. Per-rule `budget_cap_usd` and project `weekly_budget_cap_usd` both checked; the more restrictive wins.
- **Auto-gen loop spawns AT MOST one scenario per rule per hourly tick.** A rule with `daily_quota=3` fires three times across the day, not all at once. Even spread, low concurrency.
- **`pick_strategy="highest_score"`** orders by `content_references.metadata_json.score` DESC — depends on ig_scraper having populated that field at import time. Falls back to `imported_at` for ties / missing scores.
- **`pick_strategy="diverse"`** is best-effort author round-robin from a 20-row top window. Not a strict round-robin across all-time.

What's deliberately stubbed (CP-M8 polish):
- TikTok scraper bridge — separate ig_scraper milestone (not blocking the publisher).
- Caption/title sourced from `plan_slots.caption_override` or `scenarios.default_caption` — CP-M6.5.
- Provider-specific error parsing (TikTok's error codes, IG's specific failure reasons surfaced as user-friendly messages) — CP-M8.
- Webhook receivers from Meta / TT for publish status — we still poll inside the worker.
- Cross-platform reuse coordination — auto-gen doesn't yet know "this reference was used 3 days ago for IG, skip for TT this week."

## CP-M6.5 + CP-M8 (selective) deliverables

What landed:
- Migration `0009_cp_m8_polish` adds `scenarios.default_caption` + `default_hashtags`, `plan_slots.caption_override` + `hashtags_override`, `content_references.content_hash` + `caption_embedding` + `curator_score` + `curator_reason`. Hashes use bytea (pgvector deferred); admin can re-type to `vector(1536)` later without breaking the schema.
- `app/services/captions.py` — `resolve(slot, scenario)` builds the publish-ready caption string using slot override → scenario default fallback. Hashtags are deduped, lowercase-normalized, prefixed with `#`, and joined with `\n\n` after the caption body.
- `workers/publish.py` — pulls the resolved caption and hands it to IG (`caption=`) or TikTok (`title=`, truncated to 150 chars per platform limit).
- `app/services/scenarios.py` — relaxed the `update` gate so caption/hashtag edits work in any state; pipeline-shape edits (scenario_json, target_variants, quality_tier) still gated to draft/pending_review.
- `app/services/scenario_progress.py` — single-call aggregate read for the admin panel (scenario row + scenes grouped by idx + variants + voiceover summary + cost summary + progress counters). Replaces 4 polled endpoints with 1.
- `app/services/dedup.py` — `hamming_distance(a, b)` + `find_near_duplicates(...)` (O(N) scan, suitable for project pools up to thousands of references; LSH for larger pools is a CP-M8.5 polish).
- `app/services/curator.py` — async `curate(project, reference)` runs the LLM (via the project's `scenario_analysis` route), parses JSON `{score, reason}`, writes the score to the row and a `generation_calls` ledger entry. Fail-open: returns `(None, None)` when no LLM route is configured. JSON parser tolerates fenced/chatter prefix.
- API additions:
  - `GET /scenarios/{id}/progress` — aggregate panel read.
  - `GET /references/{id}/dedup-check?max_distance=N` — top-10 near-duplicates by Hamming distance on `content_hash`. Returns `has_hash: false` when CP-M8.5 hash population hasn't run yet.
  - `POST /references/{id}/curate` — synchronous curator run, returns the new score+reason.
- Tests (18 new, **137/137 green**):
  - `test_captions.py` — slot override wins, scenario fallback, empty-state, hashtag dedup, slot+scenario hashtag interplay, `_coerce_hashtags` whitespace handling.
  - `test_dedup.py` — identical=0, single-bit=1, full-byte=8, unequal-length=-1, None=-1.
  - `test_curator_parse.py` — plain JSON, score clamping (>1 → 1, <0 → 0), chatter prefix, garbage returns None, missing-score returns None.
  - Smoke covers the 3 new routes.
- ADMIN_API_GUIDE.md updated with the new endpoints + the `progress` polling pattern.

What works now (verified end-to-end against the running stack):
- `pytest tests/` → 137/137.
- Migration 0009 applied via `alembic upgrade head`.
- `GET /openapi.json` → 71 paths total.
- Captions resolve from slot → scenario → empty correctly.

Critical contract decisions (don't re-debate):
- **Caption resolution is in `app/services/captions.py`, not on the model.** Tests hit the pure function directly. The publisher worker is the only caller today; CP-M8.5 admin "preview caption" endpoint would also call it.
- **Caption / hashtag edits bypass the pipeline state machine gate.** Admins regularly tune copy AFTER images and audio are done; freezing copy at `approved` would force pointless regenerates.
- **Aggregate progress endpoint is an explicit shortcut, not a replacement.** Individual `/scene-renders`, `/render-variants`, `/generation-calls` endpoints stay — they support filters and pagination; `/progress` is a snapshot. Both have their place.
- **Dedup uses Hamming distance on `content_hash` (bytea), not pgvector cosine.** pgvector is reserved for the caption_embedding column for CP-M8.5 / CP-M9 when we add semantic dedup. Today's perceptual hash + bytewise distance is enough for "is this the same video, just re-uploaded?"
- **Curator hits the same LLM route as the analyzer (`scenario_analysis`)** — admin doesn't have to set up a separate route. If admins want a cheaper model for curating, they add a project-scoped route with `task_key='curator'` and the curator service starts using it without a code change (currently it falls through to scenario_analysis).
- **Curator is admin-triggered for now**, not auto-run on every import. CP-M8.5 will add a hourly loop that runs the curator across `inbox/candidates`. We have the service; the loop is one cron tick.
- **`content_hash` population is CP-M8.5.** The schema column exists, the dedup endpoint is wired, but the import pipeline doesn't compute hashes yet. Reference dedup-check returns `has_hash: false` until the import path runs `imagehash.phash` on a representative video frame.

What's deferred to later CP-M8/M9:
- Pgvector cosine similarity for caption embeddings.
- Auto-run curator + auto-archive low-score references on import.
- Webhook receivers from Meta / TikTok for publish status (we still poll).
- Suno / Udio music generation provider (today music is library-only).
- Outpaint-based 9:16 → 16:9 cross-aspect rendering.
- WebSocket / SSE replacement for the `/progress` polling.
- User-level auth (single static API key for now).

## CP-M8.5 deliverables

What landed:
- 3 new tables in migration `0010_cp_m85_auth`:
  - `users` — `email` (unique), `password_hash` (argon2id), `name`, `role ∈ {admin, member}`, `status ∈ {active, disabled}`, `last_login_at`.
  - `project_memberships` — `(user_id, project_id, role)` UNIQUE per pair. Per-project role: `owner` / `editor` / `viewer`.
  - `auth_sessions` — refresh token store. `token_hash` is SHA-256 of the raw token; raw token only lives client-side. `revoked_at` for logout / admin disable.
- `app/core/auth.py` — pure crypto helpers. Argon2id password hashing (`hash_password`, `verify_password`, `needs_rehash`), HS256 JWT (`issue_access_token`, `decode_access_token`), refresh token issuance + hash. `TokenError` raised on signature/expiry/malformed. Refuses placeholder `CP_JWT_SECRET`.
- `app/services/auth.py` — `login` / `refresh` / `logout`. Refresh rotates: each refresh revokes the prior session row and creates a new one. Failed login returns 401 ("invalid credentials"); disabled account returns 403.
- `app/services/users.py` — full user + membership CRUD with email-unique 409 handling.
- `app/services/bootstrap.py` — on startup, if users table is empty AND `CP_BOOTSTRAP_ADMIN_EMAIL` + `CP_BOOTSTRAP_ADMIN_PASSWORD` are set, creates the admin row. Soft-fails (logs warning) if the table doesn't exist yet (first migration run).
- `app/api/v1/deps.py` rewritten — `Principal` dataclass with `kind ∈ {user, service}`, `require_auth` accepts EITHER `Authorization: Bearer <jwt>` OR `X-API-Key: <key>`, `require_global_admin` for user-management endpoints, `require_project_role(min_role)` factory for project-scoped role gates. `get_project` enforces membership for user principals (404 on no-membership; we don't leak existence). Service principals bypass scoping.
- API additions:
  - `POST /auth/login`, `/refresh`, `/logout` (204), `GET /me`, `POST /change-password`
  - `POST/GET/PATCH/DELETE /users[/{id}]` — global admin only
  - `GET/POST/PATCH/DELETE /projects/{pid}/members[/{user_id}]` — project owner or global admin
  - All existing endpoints accept JWT or X-API-Key (legacy `require_api_key` is now a compat shim over `require_auth`).
- `app/main.py` lifespan — calls `ensure_admin()` on startup so dev runs `docker compose up` and gets a working login without a manual seed step.
- `pyproject.toml` — added `pyjwt>=2.10.0` and `argon2-cffi>=23.1.0`.
- Tests (8 new, **145/145 green**):
  - `test_auth_core.py` — password round-trip, password rejection (short), JWT issue+decode, garbage rejection, signature mutation rejection, refresh-token-as-access rejection, refresh token uniqueness + hash determinism.
  - Smoke covers 3 new tables + 7 new route prefixes.
- Documentation:
  - `ADMIN_API_GUIDE.md` § 1.2 (auth modes), § 9.8 (auth/users endpoints + token lifecycle + bootstrap).
  - `.env.example` extended with CP_JWT_*, CP_BOOTSTRAP_ADMIN_* keys.

What works now (verified end-to-end against the running stack):
- `pytest tests/` → 145/145.
- Migration 0010 applied; bootstrap admin auto-created on startup.
- `POST /auth/login` returns valid JWT; `/auth/me` resolves the user.
- Three auth paths verified:
  - JWT → 200 (project list returned)
  - No auth → 401
  - X-API-Key → 200 (legacy still works)

Critical contract decisions (don't re-debate):
- **Argon2id, not bcrypt.** Modern KDF; `argon2-cffi` is mature. `needs_rehash` is checked on every login so cost params can be tightened in prod without forced password resets.
- **Refresh tokens rotate.** Each `/refresh` issues a new pair and revokes the old session row. Stops replay on token leak, costs nothing.
- **Access tokens are stateless JWTs**, not DB rows. Adds a single `users` lookup per request (we need `user.status`) but no session table read.
- **Bootstrap is idempotent.** Re-running it with users present is a no-op. Admin can rotate `CP_BOOTSTRAP_ADMIN_PASSWORD` in env without affecting the existing row (only fires on empty table).
- **`X-API-Key` legacy mode stays.** Workers, scheduler, ig_scraper bridge use it. Future "service tokens" (per-service JWTs) is a CP-M9 polish; today the static key is enough.
- **Service principals bypass project-membership checks.** Workers need to operate any project regardless of who's logged in. Same for cron jobs and the scraper bridge.
- **`require_project_role(min_role)` is a factory dependency**, not a class. Easier to read at the route level (`Depends(require_project_role("owner"))`) and trivially testable.
- **No audit log yet.** User said it's not required for v1. Schema columns (`actor_user_id` on critical tables) can be added in CP-M9 without behavioral change.
- **Email is normalized lowercase** at write time (`get_by_email` and `create`). Pydantic `EmailStr` validates format; `.test`-style reserved TLDs are rejected by the validator.

What's deliberately stubbed:
- Forgot-password / email reset flow — out of scope (no email sender configured). Admins reset by editing the user row via `PATCH /users/{id} {password}`.
- 2FA / WebAuthn — CP-M9 if needed.
- Audit log — CP-M9 if needed.
- "Sign out other devices" — schema supports it (`auth_sessions.user_id` index), endpoint not exposed.
- Per-service tokens (instead of one static `CP_API_KEY`) — CP-M9.

## Open questions (still unresolved — flag if a milestone touches one)

1. Default reference intake action (`auto_import` vs `queue_for_review`) — user said "I'll decide".
2. M8 score threshold for auto-intake — needs distribution data from production scraper.
3. Webhook vs polling for Seedance — first version polls; revisit in CP-M4.
4. Soft-delete vs hard-delete for projects — unresolved; soft preferred for now.
5. KMS vs Fernet for `social_accounts.credentials_encrypted` — Fernet OK for dev, KMS later.
