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
| M5.5 | MCP read surface | ⏳ not started | — |
| M6 | Hashtag scan + author enrichment | ✅ done | _pending commit_ |
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

Next milestone (M5.5): MCP read surface. Mount FastMCP at `/mcp` on the same FastAPI app. Tools: `search_posts`, `get_user_profile`, `get_user_top_posts`, `get_recent_stories`, `get_post_comments`, `list_tracked_targets`, `get_job_status`. Resources: `ig://user/{username}/profile`, `ig://post/{id}`, `ig://hashtag/{name}/top`. Auth via the same `IG_SCRAPER_API_KEY` (Bearer header for MCP). Read § 8c of the plan first.

## Open questions (still unresolved — flag if a milestone touches one)

1. Multi-tenancy (`tenant_id` on jobs/targets/posts) — not yet decided.
2. Auto-promote vs `pending_review` default for hashtag-discovered users.
3. Story retention policy (currently keep forever).
4. Whether to skip `user_clips` for accounts whose feed already mixes
   reels.
