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
| M1 | Foundations | ✅ done | _see git log_ |
| M2 | Account & proxy management | ✅ done | _see git log_ |
| M3 | Job queue + worker | ✅ done | _see git log_ |
| M4 | Feed scrape flows | ✅ done | _see git log_ |
| M5 | Stories & highlights | ✅ done | _pending commit_ |
| M5.5 | MCP read + write surface | ✅ done | _pending commit_ |
| M6 | Hashtag scan + author enrichment | ✅ done | _see git log_ |
| M7 | Scheduler & tracked targets | ✅ done | _see git log_ |
| M8 | Scoring & analytical views | ✅ done | _pending commit_ |
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

## M2 deliverables

What landed in M2:
- `app/services/crypto.py` — Fernet wrapper (`encrypt`/`decrypt`/`encrypt_optional`/`decrypt_optional`). Refuses to start with the placeholder `IG_SECRET_KEY`. Raises a clear `RuntimeError` with rotation guidance if a stored ciphertext fails to decrypt under the current key.
- `app/services/instagrapi_client.py` — wraps `instagrapi.Client` with proxy URL building (`build_proxy_url`), session-blob loading, and a `login_account(account, proxy, verification_code=None)` coroutine that runs the blocking login in a threadpool. Maps instagrapi exceptions to the `ig_accounts.status` enum (`active` / `challenge_required` / `banned` / `disabled`).
- `app/services/accounts.py` — service-layer CRUD: `create_account`, `list_accounts`, `get_account`, `update_account`, `disable_account`, `run_login`. Whitelisted role/status/quota_tier values; 409 on username conflicts; 400 on invalid proxy_id.
- `app/services/proxies.py` — service-layer CRUD plus `test_proxy(...)` that issues a single `GET https://api.ipify.org?format=json` through the proxy with an 8s timeout, persists `last_ok_at` / `failure_count` / `status` accordingly, returns latency + public IP.
- `app/schemas/accounts.py` and `app/schemas/proxies.py` — request/response Pydantic models. **Read shapes never expose `password_enc` or `session_blob`.**
- `app/api/v1/accounts.py` — POST/GET/GET-by-id/PATCH/POST-disable/POST-login. POST-login is async; the rest are sync (DB-bound).
- `app/api/v1/proxies.py` — POST/GET/GET-by-id/PATCH/POST-test.
- `tests/test_crypto.py` — 5 tests covering round-trip, optional round-trip, IV randomness, key rotation failure, placeholder rejection. All pass.

What works now (verified):
- All 33 routes register (was 27 in M1, +11 actual M2 endpoints replacing 5 stubs).
- Crypto round-trips Unicode passwords cleanly.
- Decrypting under a rotated key raises a clear error instead of silently returning garbage.
- The service-layer functions are pure (no FastAPI imports) — easy to unit-test.

What's deliberately NOT in M2 (still stubbed):
- The account *pool* (`account_pool.acquire()` / `release()` with `SELECT ... FOR UPDATE SKIP LOCKED`) — that lives next to the worker loop and lands in M3.
- Daily-quota enforcement, active-hours filtering, cooldown gating — those decisions live inside the pool, not on the account model. Same milestone.
- Canary-account scheduled probe — the `role='canary'` column is honoured by `accounts_service.create_account`/`update_account`, but the actual canary scheduled job lands in M7 (scheduler).

## M3 deliverables

What landed in M3:
- `app/services/heartbeat.py` — atomic upsert into `ig_worker_heartbeat` (`ON CONFLICT (process, instance_id) DO UPDATE SET last_seen_at`). `make_instance_id()` builds a stable id from hostname + pid.
- `app/services/usage.py` — single-statement upsert into `ig_usage_daily` with column-wise `+=` semantics. Two workers can bump the same row concurrently without losing writes.
- `app/services/jobs.py` — full CRUD + worker primitives. The load-bearing query is `claim_next_job` (`UPDATE ... WHERE id = (SELECT FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING id`). `mark_succeeded` / `mark_failed` / `mark_retry` handle the state transitions including max-attempts → terminal failure.
- `app/services/account_pool.py` — `acquire(session, job)` / `release(session, acquired, outcome)`. Pre-filters at SQL level (`status='active'`, `cooldown_until` past, role match), then Python-side post-filter for active-hours (timezone aware via `zoneinfo`) and tier-quota (today's `ig_usage_daily.calls_made` vs `IG_DAILY_QUOTA_<tier>`). Outcomes drive cooldown durations from § 5.3 (I) of the plan.
- `app/services/scrapers/__init__.py` — registry-based dispatcher. Real scrapers (M4–M6) call `register(job_type, fn)` at import. Until then, `_stub_scraper` sleeps 1–3s and reports zero work — enough to prove the queue under load.
- `app/worker.py` — replaces the placeholder. Spawns `IG_WORKER_CONCURRENCY` async loops + 1 heartbeat task. SIGTERM/SIGINT installs an asyncio.Event so loops finish their current job and exit cleanly.
- `app/api/v1/jobs.py` — POST/GET/GET-by-id/POST-cancel/POST-retry. Filters: `status`, `job_type`, `target`, `limit`, `offset`. All gated by X-API-Key.
- `app/schemas/jobs.py` — `JobCreate`, `JobRead`. `JobType` and `JobStatus` are typing.Literals; a test enforces they stay in sync with `VALID_JOB_TYPES` / `TERMINAL_STATUSES` in the service module.
- `tests/test_account_pool.py` — 7 tests covering active-hours window, timezone awareness, wrap-around windows (e.g. 22..6), weekday bitmap, role detection, cooldown ranges.
- `tests/test_jobs_validation.py` — 2 tests that prevent JobType drift between the schema and service.

What works now (verified):
- 33 routes including 5 real M3 endpoints replacing the stubs.
- 14/14 unit tests green.
- Worker launches `IG_WORKER_CONCURRENCY` loops + heartbeat, handles SIGTERM cleanly.
- Stub scraper proves the queue/pool/heartbeat plumbing without IG calls.

Critical contract decisions made in M3 (don't re-debate without explicit OK):
- Worker concurrency is **in-process**, not multi-replica. Set `replicas: 2+` in compose to scale horizontally; `SKIP LOCKED` makes that safe out of the box.
- Account locking uses `last_used_at NULLS FIRST, RANDOM()` so newly onboarded accounts get exercised but normal load is even.
- Active-hours / quota / cooldown checks are **post-fetch in Python**, not SQL. Trade-off documented at top of `account_pool.py` — keeps the SQL portable, costs at most one extra row read per skip.
- Soft-fail (no retries left) → terminal `failed`; `cancel` is allowed only on non-terminal jobs.
- `mark_retry` uses `attempt >= max_attempts` to decide when to give up. `attempt` was already incremented at claim time, so the first failure of a job with `max_attempts=3` would give `attempt=1, max=3` → retry; the third would give `attempt=3, max=3` → terminal.

What's still stubbed:
- Real scrapers (`_stub_scraper` is everything for M4–M5).
- Tracked-targets registry (`/targets`) and the scheduler — M7.
- Score computation (M8), webhooks (M9).

## M4 deliverables

What landed in M4:
- `app/services/simhash.py` — 64-bit Charikar simhash from caption tokens. ~30 LOC, no external deps. Round-trip helpers `to_signed_64`/`from_signed_64` because Postgres BIGINT is signed.
- `app/services/features.py` — `extract(caption)` returns `CaptionFeatures` with language (langdetect), emoji_count (Unicode-range regex), hashtag/mention extraction, has_question (`?` + Turkish particles `mi/mı/mu/mü`), has_cta (TR + EN imperative regex), simhash. Empty/None captions short-circuit to zeros.
- `app/services/filters.py` — `passes_filter(...)` with the three-clause rule from § 7. `play_count` preferred over `view_count` for the impressions check; both null = treated as 0 (so photo-only targets with `min_impressions` set skip everything, as documented).
- `app/services/throttle.py` — `human_delay(action)` lognormal-clipped per `_range_for(action)`, `micro_jitter()` for inside-loop wobble, `Throttle` class with macro-pause + long-break logic from § 5.3 A–C.
- `app/services/persistence/` package — `upsert_ig_user`, `upsert_hashtag`, `upsert_audio_track`, `upsert_post`, `upsert_comments`. All single-statement `INSERT ... ON CONFLICT DO UPDATE` idioms; `upsert_post` writes the metric snapshot inline, materialises hashtag rows, and stores feature columns.
- `app/services/scrapers/user_feed.py` — `run_user_feed_full` and `run_user_feed_incremental`. Walk both `user_medias_paginated_v1` and `user_clips_paginated_v1`, merge by pk, apply filter, persist (post + snapshot + hashtags + audio), optionally fetch comments. Cursor update at end via `ig_scan_targets.last_seen_post_id` / `last_seen_taken_at` / `next_run_at` (with ±jitter from `IG_TARGET_INTERVAL_JITTER_PCT`).
- `app/services/scrapers/__init__.py` — registers both scrapers with the dispatcher.
- New tests: `test_features.py` (8 tests, TR + EN coverage), `test_filters.py` (6), `test_throttle.py` (3). All deterministic — `asyncio.sleep` is patched in throttle tests so they run in milliseconds.

What works now (verified):
- 31/31 unit tests green (5 crypto + 7 account_pool + 2 jobs_validation + 8 features + 6 filters + 3 throttle).
- `import app.services.scrapers` registers both real scrapers; the dispatcher hits them, not the stub.
- Caption features handle Turkish: `mi` particle catches questions without `?`, biography/CTA patterns include `biyodaki`, `paylaş`, `kaydet`, etc.
- Simhash determinism guaranteed (md5-seeded), Hamming distance correctly orders near-vs-far captions.

Critical contract decisions made in M4 (don't re-debate):
- **Reels merge is unconditional**: full backfill walks both `user_medias` AND `user_clips` and dedupes by pk. Some accounts post reels-only; this catches them at the cost of one extra paginator pass.
- **Cursor-stop is conservative**: incremental walker stops on EITHER `last_seen_post_id` match OR `taken_at <= last_seen_taken_at`. The taken_at fallback handles the "pinned/deleted post drops out of feed" case so we never re-scrape from the start by accident.
- **Comments fetched only for posts that pass the filter**, plan § 6.2. Saves the per-post comment API call on rejected posts.
- **Persistence is per-post-transactional**: each `upsert_post` runs in its own `session_scope` so a single bad media payload can't roll back already-persisted siblings. Trade-off: more commits, slower; can batch later if profiling demands.
- **Job-typed media URLs**: `media_urls` is a JSONB array of all CDN URLs (video_url, thumbnail_url, image_versions2 candidates, carousel children). Order best-effort; downstream consumers should not rely on it.
- **`raw` columns hold the full instagrapi payload** so we can re-derive features without re-scraping when the extractor improves.

What's still stubbed:
- `user_stories` and `user_highlights` (M5).
- Hashtag scrapers + author enrichment (M6).
- Tracked-targets registry + scheduler (M7).
- Score (M8). Snapshots are being written; computation isn't.
- Webhooks (M9).

## M5 deliverables

What landed in M5:
- `app/services/persistence/stories.py` — `insert_story(...)`. INSERT-only with `ON CONFLICT (id) DO NOTHING`. Walks instagrapi sticker payloads to pull hashtags, mentions, swipe-up link. `expires_at` computed as `taken_at + 24h` when IG doesn't supply one explicitly.
- `app/services/persistence/highlights.py` — `upsert_highlight(...)` and `link_highlight_item(...)`. Highlights are containers; their items reuse `ig_stories` so analytics queries work the same way as for live stories.
- `app/services/scrapers/user_stories.py` — calls `user_stories_v1`, persists each into `ig_stories`. No filter, no comments, no pagination — IG returns the whole tray in one shot.
- `app/services/scrapers/user_highlights.py` — calls `user_highlights` + `highlight_info` per container, persists items via the stories pipeline plus the membership row.
- `tests/test_stories_persistence.py` — 5 tests covering datetime coercion (unix epoch, naive datetime, None, garbage) and sticker walker dedup.

## M6 deliverables

What landed in M6:
- `app/services/scrapers/hashtag.py` — `run_hashtag_top` and `run_hashtag_recent`. Calls `hashtag_medias_top_v1` / `_recent_v1`, applies the same filter as user_feed, persists posts through the same pipeline (post + snapshot + audio + hashtags). Comments are intentionally NOT fetched here — too noisy at hashtag scale.
- `app/services/scrapers/enrichment.py` — `enrich_authors(...)` co-routine. For each unique author seen in a hashtag scan: skip if already a tracked target, refresh profile if `last_seen_at` older than 7 days, then check `_should_promote(profile, min_followers, min_media)`. On promotion auto-creates an `ig_scan_targets` row with `auto_discovered=true` and `source_target_id=` job's scan_target_id. Status = `pending_review` by default; `active` when `IG_AUTO_PROMOTE_DISCOVERED=true`.
- `app/services/scrapers/__init__.py` — registers all 6 scrapers in the dispatcher: feed full / incremental, stories, highlights, hashtag top / recent.
- `tests/test_enrichment.py` — 5 tests pinning the promotion thresholds (followers, media count, private flag, missing fields).

What works now (verified):
- 41/41 unit tests green.
- Dispatcher registry: `['hashtag_recent', 'hashtag_top', 'user_feed_full', 'user_feed_incremental', 'user_highlights', 'user_stories']`.
- Module imports clean — no circular import between user_feed (helpers) and the M5/M6 scrapers that reuse them.

Critical contract decisions made in M5+M6:
- **Stories are insert-only**. We never UPDATE an `ig_stories` row. Missed runs lose data permanently — that's by design (matches IG semantics) and documented at the top of `persistence/stories.py`.
- **Highlight items reuse `ig_stories`**. Saved highlights ARE stories from the user's perspective; one analytics query (e.g. "all stories of brand X") trivially covers both.
- **Hashtag jobs don't fetch comments by default**. Plan § 6.5 noted "too noisy at scale" — the cost / signal trade-off only makes sense for tracked-user feeds where the per-author signal is high.
- **Enrichment dedupes per scan**. If 30 posts in a hashtag batch are by the same author, we still only call `user_info_v1` once for them. Same author appearing in a future scan within `ENRICH_REFRESH_DAYS=7` is a no-op.
- **Promotion is conservative**. Three gates (followers / media / not private) AND default `pending_review`. The plan's median-score gate from § 6.6 lands in M8 — flagged in `enrichment.py` so we know to add it.
- **Auto-discovered targets carry provenance** via `source_target_id`. Operators can ask "where did this brand come from?" with a single SELECT.

What's still stubbed:
- `user_enrich` job_type — not registered. The current enrichment runs INSIDE hashtag scrapes; a standalone `user_enrich` job (refresh stats for a known target outside the daily scan) is a future polish item, not Phase 1 critical.
- M5.5 (MCP read surface), M7 (scheduler + tracked-target CRUD), M8 (scoring), M9 (webhooks), M10 (hardening).

## M7 deliverables

What landed in M7:
- `app/schemas/targets.py` — `TargetCreate`, `TargetUpdate`, `TargetRead`, plus literals `TargetKind`, `TargetStatus`, `HashtagSection`.
- `app/services/targets.py` — full CRUD + the scheduler primitive `enqueue_jobs_for_due_targets()` (uses `SELECT FOR UPDATE SKIP LOCKED`, decides job_type per target, bumps `next_run_at` with ±jitter), plus `run_now` for operator-triggered immediate runs and `activate`/`pause` for the lifecycle.
- `app/api/v1/targets.py` — POST/GET/GET-by-id/PATCH/POST-activate/POST-pause/POST-run-now (replaces the M1 stubs).
- `app/scheduler.py` — replaces the placeholder. Heartbeat task + tick task; tick body calls `enqueue_jobs_for_due_targets` once per `IG_SCHEDULER_TICK_SECONDS`. SIGTERM/SIGINT graceful shutdown.
- `tests/test_targets_logic.py` — 9 tests pinning the job-type selection rules (`first_backfill_done` → full vs incremental, fetch_stories on/off, fetch_highlights only on first run, hashtag top vs recent), the jitter band, and value normalisation.

## M5.5 deliverables

What landed in M5.5:
- `app/services/queries.py` — read-only helpers (`search_posts`, `get_user_profile`, `get_user_top_posts`, `get_post_comments`, `get_recent_stories`, `get_job_status`). Use the read-replica engine when configured.
- `app/mcp_server.py` — FastMCP instance with 7 read tools and 5 write tools. Lazy-imports `mcp` so the API process still starts cleanly when the package is missing.
- `app/main.py` — mounts `mcp_server.streamable_http_app()` at `/mcp` inside a `try/except` so a FastMCP transport-API change can't crash the API.
- `app/mcp_stdio.py` — `python -m app.mcp_stdio` for local Claude Desktop / Inspector attachment.

What works now (verified):
- 50/50 unit tests green.
- API process logs `mcp_server_mounted path=/mcp` at startup.
- Scheduler entry point loads cleanly; heartbeat hooks ready.
- `app.main.app.routes` shows `/mcp` mounted as a sub-app alongside `/api/v1/...`.

Critical contract decisions made in M5.5+M7:
- **Scheduler is single-process**: `replicas: 1` in compose. SKIP LOCKED would make multiple replicas safe but also pointless — one tick per minute over the full target table is cheap.
- **Job type per target**: see `_job_types_for_target()`. First-run users get `user_feed_full` + `user_stories` + (optional) `user_highlights`. Subsequent runs get `user_feed_incremental` + `user_stories`. Hashtag targets get exactly one job (`hashtag_top` or `hashtag_recent`).
- **Highlights default to "scan once on first add"** — they don't change frequently, so re-scanning daily is wasteful. Operator can `POST /targets/{id}/run-now` for a manual highlight refresh, or M10 polish can add a separate cadence.
- **MCP tool surface is curated, not 1:1 with REST**: account/proxy CRUD is REST-only. An LLM agent that gets prompt-injected by scraped content cannot delete a scraping account through MCP — that's deliberate.
- **Same API key for MCP and REST**: `IG_SCRAPER_API_KEY` as `X-API-Key` for REST, as `Authorization: Bearer` for MCP. One secret to rotate.
- **MCP server is optional at import time**: `_build_server()` returns None if `mcp` isn't installed; the API process logs a warning and continues. Keeps the dev story flexible.
- **Read tools use the read-replica engine** when configured (`POSTGRES_READ_REPLICA_DSN`). Write tools always go to the primary.
- **Canary scheduled probe** is deferred to M10 (hardening). The plumbing is all there (`role='canary'` honoured by the pool, `params.canary` recognized), just no automatic hourly probe yet.

What's still stubbed:
- Score (M8). Snapshots and a pretend-score column exist in `ig_posts`; no computation yet.
- Webhooks (M9).
- Hardening / canary scheduled probe / Grafana dashboard (M10).
- `user_enrich` standalone job — the enrichment runs INSIDE hashtag scrapes (M6); a standalone job to refresh stats for a known target is a future polish item, not Phase 1 critical.

## M8 deliverables

What landed in M8:
- `app/services/scoring.py` — `compute_score()` (pure function), `velocity_for_post()`, `author_relative_score()`, `update_post_score()`, `recompute_recent_batch()`, `refresh_views()`. Six components, all in [0,1], weighted via env. Final score clipped to [0, 100]. Inline recompute is wrapped in try/except so a scoring blip can't kill a scrape.
- `alembic/versions/0002_scoring_views.py` — three materialised views with unique indexes (so REFRESH MATERIALIZED VIEW CONCURRENTLY works without holding an exclusive lock):
  - `ig_top_posts_by_author` — per-author rank by score
  - `ig_author_posting_pattern` — (hour_of_day, weekday) histogram with avg_score
  - `ig_hashtag_velocity` — last-7d vs prior-7d post counts and avg_score per hashtag, plus a `post_delta` column
- `app/services/persistence/posts.py` — every `upsert_post` now triggers an inline score recompute after the snapshot is written.
- `app/scheduler.py` — added `_daily_loop` task that fires at 03:00 UTC, runs `recompute_recent_batch(days=30)` then `refresh_views(concurrently=True)`. Idempotent across restarts (date-keyed in-memory marker).
- `app/api/v1/posts.py` — new `/api/v1/posts` router. Filters: `author`, `hashtag`, `min_likes`, `min_play_count`, `min_score`, `since`. Sort: `taken_at_desc | score_desc | likes_desc | play_count_desc`. Plus `/posts/{post_id}/comments`.
- `app/api/v1/api.py` — registers the posts router.
- `app/mcp_server.py` — new `get_high_scoring_posts` tool. Combines author/hashtag/since/min_score + score-desc ordering — the canonical "show me what's working" query the AI generator will hit.
- `app/services/scrapers/enrichment.py` — median-score gate activated. `_median_recent_score()` returns the median of the candidate's last 10 scored posts (None when <5). `_should_promote()` now takes `median_score` + `min_score` and rejects below `IG_MIN_SCORE_FOR_ENRICH` when we have a meaningful sample. Sample-too-small passes through (we don't punish users we just discovered).
- `tests/test_scoring.py` — 7 tests pinning the formula (zero-engagement floor, perfect-input ceiling, freshness decay, view_efficiency video-vs-photo, [0,100] clipping, component completeness, velocity normalisation).
- `tests/test_enrichment_score_gate.py` — 5 tests covering the M8 gate (small-sample passthrough, high-score pass, low-score block, env-disabled no-op, deterministic gates still fire).

What works now (verified):
- 62/62 unit tests green (was 50; +12 in M8).
- `alembic upgrade head --sql` emits valid CREATE MATERIALIZED VIEW + CREATE UNIQUE INDEX statements for all three views.
- API mounts `/api/v1/posts` and `/api/v1/posts/{id}/comments`; MCP exposes `get_high_scoring_posts`.

Critical contract decisions made in M8:
- **Score is recomputed on every upsert**, not just periodically. Cost is one query per upsert (≤6 small reads + 2 updates), acceptable at scrape rates. Optimisation opportunity: cache the author's median across posts of the same scrape session — flagged for a future polish.
- **Component normalisation thresholds** are constants in `scoring.py` (`_ENGAGEMENT_RATE_CAP=0.5`, `_VELOCITY_CAP=100`, `_VIEW_EFFICIENCY_CAP=0.10`, `_COMMENT_INTENSITY_CAP=0.05`). Calibrated from "what does a viral post in our niche look like"; if these turn out wrong we tune in code, not env, because they're not what an operator should fiddle with.
- **`author_relative` returns 0.5 (neutral) when <3 posts in author's history**. Avoids a self-fulfilling-prophecy effect where a brand-new account's first post gets unfairly low/high relative scores.
- **`view_efficiency` is 0.0 for photos** (no `play_count`/`view_count`). This is by design; photos compete on engagement_rate, comment_intensity, and author_relative, not views.
- **Daily recompute runs at 03:00 UTC** (`_DAILY_HOUR_UTC`). Hardcoded — not env-configurable — because it's an internal operational detail and changing it requires re-thinking the daily fleet's timing anyway.
- **Materialised views refresh CONCURRENTLY**. Requires a unique index on each (we created them in the migration). Reads stay live during refresh.
- **Score is stamped on the latest snapshot too**, so historical analytics can plot score curves over time without re-running the formula.

What's still stubbed:
- Webhooks (M9).
- Hardening + canary scheduled probe + Grafana dashboard + final anti-detection tuning (M10). End of Phase 1.
- All Phase 2 work (M11–M13).

Next milestone (M9): webhooks + retention. New `ig_webhooks` dispatcher with HMAC signing, retry/backoff. Triggers on score-threshold crossings (`post_score_threshold`) and tracked-target completions (`target_run_completed`). GDPR `expires_at` columns wired on `ig_comments.text` and `ig_users.biography`; nightly nullifier job present but disabled by default. Read § 14c (items 7+9) of the plan first.

## Open questions (still unresolved — flag if a milestone touches one)

1. Multi-tenancy (`tenant_id` on jobs/targets/posts) — not yet decided.
2. Auto-promote vs `pending_review` default for hashtag-discovered users.
3. Story retention policy (currently keep forever).
4. Whether to skip `user_clips` for accounts whose feed already mixes
   reels.
