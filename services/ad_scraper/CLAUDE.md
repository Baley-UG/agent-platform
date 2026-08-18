# ad_scraper — Claude context file

> Read this **before** doing any work in `services/ad_scraper/`. It is the
> single-page brief a new session needs to pick up where the last one left
> off. `README.md` is the operator's guide; this file is for *what's done,
> what's decided, and what's next*.

## What this service is

A FastAPI microservice that ingests competitor ad creatives from
AppGrowing/YouCloud's GraphQL API (`materialList`), normalises them into the
**shared main Postgres** under the `ad_*` table prefix, mirrors their media
into the shared S3 bucket before the source URLs expire, and exposes the
result over REST. Feeds `content_pipeline` as generation references.

Two processes from one Docker image:
- `app.main` — FastAPI control plane, port 8083 (`/api/v1/...`).
- `app.worker` — asyncio worker that claims jobs from `ad_scrape_jobs` via
  `SELECT ... FOR UPDATE SKIP LOCKED`.

No scheduler. No Redis. No Celery. No separate database.

## Locked-in design decisions (do NOT re-debate without explicit user OK)

- **Shared Postgres, `public` schema, `ad_*` prefix** — same call ig_scraper
  made, not a separate DB and not a separate schema.
- **Own Alembic version table: `ad_alembic_version`.** ig_scraper owns
  `public.alembic_version`; sharing it makes each service read the other's
  revision id as unknown. `alembic/env.py` also filters autogenerate to
  `ad_*` so it never proposes dropping ig_scraper's tables.
- **Ingestion is operator-driven.** No saved-search registry, no cadence, no
  scheduler process. One job = one filter set + one page window.
- **Error classification reads the response body, not the status code.** The
  endpoint answers HTTP 200 for auth, plan and filter failures alike.
  Branching is on the `extensions.c` **family** (`05:` = session), because
  two distinct session codes were observed (`05:400001` malformed,
  `05:403001` expired) and a third is likely.
- **Page ceiling is enforced client-side.** `page > 200` is refused before a
  request is spent; a filter set can never yield more than 10 000 rows.
  Truncation is reported on the job (`stats.truncated`), never silent.
- **Facets are one generic `(kind, code)` pair of tables**, not six lookup +
  six join tables. Costs per-facet FK typing, buys migration-free absorption
  of a seventh facet.
- **`kind` on `ad_advertisers` comes from GraphQL `__typename`**, which our
  query requests explicitly and which is **confirmed working against the live
  endpoint** (52 `AppBrand` entries on one page). Do not go back to inferring
  it from payload shape — that heuristic is undocumented and fails silently
  on a new union member. NULL when absent; a NULL is visibly missing, a wrong
  guess isn't.
- **App-id filtering goes through `searchDsl`, never `campaign`.** `JobCreate`
  exposes `app_id` and compiles the DSL entry; a numeric `filters.campaign`
  is rejected. See the verified-facts table for why that guard exists.
- **Mirror policy defaults to `always`.** YouCloud URLs die ~15 days out, so
  mirroring is the reason the service exists. Unknown policy values fail
  *open* (mirror anyway) because a skipped mirror is unrecoverable.
- **`job.mirror` is tri-state and an explicit value beats the policy.** NULL
  follows `AD_MIRROR_MEDIA`; `false` opts out even under `always`; `true`
  opts in under `job`; `never` ignores it. It used to be ignored entirely
  outside `job` mode — the API accepted `mirror: false` and mirrored anyway,
  the same "accepted then quietly did the opposite" shape as the `campaign`
  zero-row trap.
- **An already-mirrored creative is never re-downloaded.** `upsert_material`
  returns the pre-existing S3 keys (via the UPSERT's RETURNING), and the job
  counts those as `stats.mirror_cached`. Re-running a filter is the normal
  way to catch new creatives; without this it re-fetched every video it
  already held.
- **Per-material transactions.** Each material upserts in its own
  `session_scope` so one malformed payload can't roll back its siblings.
- **`raw` JSONB holds the full payload** so a mapping fix is a backfill, not
  a re-scrape of 10 000 rows.
- **Auth is a cached session token; there is no login flow and no stored
  password.** Automatic login was implemented as a seam, then dropped on the
  user's call ("login kısmı sıkıntılı, cache token kullanalım"): it meant
  replaying a password against a flow we cannot inspect (introspection is
  disabled), on a platform whose ToS it may breach, with account lockout as
  the failure mode — to save a weekly paste. `ad_credentials` has no
  username/password columns. Do not add them back without an explicit
  decision.
- **`AuthExpired` is terminal, not retried.** Only an operator can mint a new
  token. The worker also calls `credentials.mark_rejected`, which drops the
  cached token and — past the threshold — flips the row to `login_failed` so
  jobs stop replaying a token the server already refused.
- **The decrypted token is cached in process memory** with its expiry, so the
  hot path costs no DB round-trip and no Fernet decrypt. Invalidated on
  store, on API rejection, and on its own expiry.

## Milestone status

Update this table at the end of each session.

| ID | Title | Status | Commit |
| - | - | - | - |
| AD-M1 | Foundations — schema, client, credentials, API | ✅ done | f677ef1 |
| AD-M2 | Ingestion — job queue, worker, persistence, mirror, read API | ✅ done | f677ef1 |
| AD-M3 | Integration — gateway, compose, content_pipeline bridge, docs | ✅ done | f677ef1 |
| AD-M4 | Automatic login | ❌ dropped by decision — token-only auth instead | — |

Status legend: ⏳ not started · 🔄 in progress · ✅ done · 🚧 blocked · ❌ dropped.

## Conventions inherited from `agent-platform`

- Python 3.13, dependency manager `uv`, `pyproject.toml` per service.
- `structlog` JSON logs, Prometheus metrics, `SQLModel` for ORM, Alembic for
  migrations, `pydantic-settings` reading the same `.env` as the main app.
- Test runner `pytest`. Linting `ruff` + `black`. Line length 119.

## How to start a fresh session

1. Read this file, then `README.md` for the operator-facing behaviour.
2. `git log --oneline -20` to see what's actually merged.
3. Pick the next ⏳ / 🚧 row above.
4. Implement, test, commit, mark the row ✅ with the commit SHA.

## Verified facts about the upstream API

Established by probing the live endpoint. Don't re-derive these.

| Fact | Detail |
| - | - |
| HTTP status | Always 200, including for auth/plan/filter errors |
| Page ceiling | `page ≤ 200`, server-fixed `limit = 50` → 10 000 rows per filter set |
| `accept-language` | Mandatory; absent → HTTP 406 with a plain-text body |
| Introspection | Disabled (`__schema` → GRAPHQL_VALIDATION_FAILED) |
| Auth surface | One cookie, `sessionId` (a JWT with an `exp` claim) |
| `total` | Drifts between calls — the feed is live; rows shift across pages |
| Session codes | `05:400001` malformed token · `05:403001` expired session |
| Plan/anon code | `00:403001` "Permission denied, please upgrade your plan" |
| CDN signature | `auth_key` IS enforced — tampering it gives 401 |
| CDN referer | NOT validated — a wrong referer still gets 200, so we don't spoof it |
| App-id filter | `searchDsl: [{"key":"appid","value":"<store id>","type":"equal"}]` — 6 559 rows for 1661308505. This is the web UI's `advanced` panel |
| `campaign` trap | `campaign: "1661308505"` returns **0 rows, HTTP 200, no error**. It takes the opaque entity id, not a store id. `JobCreate` rejects a numeric `campaign` with a 422 for exactly this reason |
| Sort values | `max_dt_desc` (default) and `impression_inc_2y_desc` both confirmed |
| Impression display | Tops out at **">10M"** — a *prefixed* value. `parse_compact_number` handles `>`/`<`/`~`/`+`; without that the highest-impression creatives parsed to NULL and fell out of every threshold and sort |
| CDN Accept | Content-negotiated: a browser Accept gets a 61 KB **webp** where no Accept gets the 156 KB **jpeg**. `_download` pins Accept to the original so the bytes match `media_format`, which comes from the API payload |

Payload traps, all handled in `app/services/parsing.py` and
`app/services/persistence/`:

- `material.duration` is **days on air**, not a video length → `run_days`.
- `creative.resource[].duration` is **seconds** → `media_duration_sec`.
- `impression_inc_2y` is a display string (`"1.1M"`) → raw + parsed bigint.
- `campaign[]` is an array of up to 66 entities → `ad_advertisers` M2M.
- `campaign[].alias` is an **array** of localised store names, not a string.
  It overflowed `varchar(512)` on the first real AppBrand row; the column is
  `text[]`. This one only surfaced in an end-to-end run.
- `material.violation` is a bare label string (`"Human Exploitation"`), so
  the column is `text`, not JSONB.
- `campaign[].developer` and `developer.id` can both be null.

## AD-M1 + AD-M2 deliverables

Schema — 8 tables, migration `0001_initial_ad_m1`:
`ad_materials`, `ad_material_resources`, `ad_dimensions`,
`ad_material_dimensions`, `ad_advertisers`, `ad_material_advertisers`,
`ad_scrape_jobs`, `ad_credentials`.

Code:
- `app/services/youcloud/{client,queries,errors}.py` — the load-bearing
  piece. Body-based error taxonomy, one session refresh + retry per call,
  page-ceiling guard, `paginate_materials` async generator. `queries.py`
  adds `__typename` to the campaign fragments and trims the selection set.
- `app/services/parsing.py` — total parsers (`parse_compact_number`,
  `expires_at_from_auth_key`, `jwt_expires_at`, `filename_from_url`). None
  raise; all return None on garbage.
- `app/services/credentials.py` — Fernet-encrypted password + cookie,
  `ensure`/`refresh` seam, lockout after N failures, manual-paste path.
- `app/services/persistence/{materials,dimensions,advertisers}.py` — single
  `INSERT … ON CONFLICT DO UPDATE` per table, pure `extract_*` mapping
  functions next to each so the mapping is testable without a DB.
  New-vs-updated is detected with `RETURNING (xmax = 0)`.
- `app/services/mirror.py` — `transfer()` does network+S3 with **no DB
  access**, `persist_keys()` does the DB write. Split on purpose: the
  transfer runs in a worker thread and a SQLAlchemy Session must not cross
  threads.
- `app/services/ingest.py` — the job runner. Per-material transactions,
  truncation reporting, `mark_ok` on the credential after a successful page.
- `app/services/jobs.py` + `app/worker.py` — SKIP LOCKED claim, graceful
  shutdown, stuck-job requeue on startup, and the error→outcome mapping
  (`AuthExpired`/`Transient`/`Transport` retry; `PlanDenied`/`BadFilter`
  terminal).
- `app/services/queries.py` — read side. Facet filters compose as AND across
  kinds, OR within a kind. Sorts are whitelisted (no user-supplied ORDER BY).
- API: `/health`, `/ready`, `/metrics`, `/credentials*`, `/jobs*`,
  `/materials*`, `/advertisers*`, `/dimensions`.

Verified (not just asserted):
- 191/191 unit tests green, no DB or network needed.
- `alembic upgrade head` applied against the live compose Postgres without
  disturbing ig_scraper's migration state.
- Fixture page ingested twice → `['new','new']` then `['updated','updated']`
  (idempotent); 26 dimensions, 4 advertisers, 32 edges written.
- Facet AND semantics confirmed (`area=JP` 2 rows, `area=JP&media=999` 0).
- Mirror round-trip against MinIO: download → S3 → presigned GET returns the
  exact bytes; query string stripped from the key; over-cap body refused.
- CP bridge: material imported as `source_provider='appgrowing'`, S3 object
  server-side copied into the project prefix, duplicate import → 409.

## AD-M3 deliverables

- `docker-compose.yml` — `ad-scraper-api` (expose 8083) and
  `ad-scraper-worker`, both from one image, `ig-scraper-*` as the template.
- Main app: `AD_SCRAPER_URL` / `AD_SCRAPER_API_KEY` in `app/core/config.py`,
  `/api/v1/ad-scraper/{path}` proxy in `app/api/v1/admin_gateway.py` (global
  admin only), and `("ad-scraper", …, "AdScraper_")` in the OpenAPI
  federation list so the routes show in the main `/docs`.
- `.env.example` — full `AD_*` block with the ceiling and mirror policy
  explained inline.
- content_pipeline: `app/services/ad_scraper_bridge.py` (cross-schema raw
  SELECT + flattened facet/advertiser name arrays),
  `ReferenceImportFromAds`, `references.import_from_ads`,
  `POST /projects/{pid}/references/import-from-ads`, and
  `s3.copy_object`. `source_provider` accepts `'appgrowing'` with **no CP
  migration** — the column is `String(32)` with no CHECK constraint.

Contract decisions made here:
- **The import copies rather than references** ad_scraper's S3 object by
  default (`copy_media=true`). Server-side copy, no bytes through the
  process, and CP's rows survive ad_scraper pruning its own prefix. A failed
  copy falls back to referencing in place rather than losing the asset.
- **`asr` becomes the reference `transcript`**, `slogan` becomes the
  `caption`. Impressions / run_days / advertisers / areas go into
  `metadata` where `auto_generation_rules.pick_strategy` can rank on them.
- **Video creatives (`material_type == 202`) enqueue the ffmpeg keyframe
  pass**, same as an imported reel.

## Open items

1. **The token must be pasted roughly weekly.** `sessionId`'s `exp` is ~7
   days out. `/ready` reports `youcloud_session`, `GET /credentials` reports
   `expires_in_seconds`, and `ad_login_failures_total` fires on rejection —
   so the reminder can be an alert rather than a habit. Nothing renews it
   automatically, by decision (see the locked-in list).
2. **Server-side session invalidation is independent of the JWT `exp`.** A
   token whose `exp` was still a week out came back `05:403001` "Login
   session has expired" — logging in elsewhere appears to rotate it. So
   `expires_in_seconds` is an upper bound, not a promise; the rejection path
   is what actually catches it.
3. **`.dockerignore` is load-bearing.** `COPY . .` runs after the Dockerfile
   builds `/app/.venv`, so a host venv in the build context silently
   overwrites it and the container dies with `exec /app/.venv/bin/uvicorn:
   no such file or directory`. Found by actually running the image. Note
   also that compose tags a separate image per role (`ad-scraper-api`,
   `ad-scraper-worker`) — rebuild both, or the worker keeps running the old
   layer.
4. **No metric snapshot history.** Deliberately out of scope: there is no
   `ad_material_metric_snapshots`, so impression/run-day trends over time
   aren't queryable. Adding it later is additive.
5. **No Grafana dashboard yet.** Counters are declared and populated
   (`ad_jobs_total`, `ad_materials_saved_total`, `ad_api_errors_total{code}`,
   `ad_login_failures_total{reason}`, `ad_mirror_bytes_total{kind}`,
   `ad_filter_truncated_total`); nothing plots them.
6. **No MCP surface.** ig_scraper exposes one; this service is REST-only.
