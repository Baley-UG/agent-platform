# Instagram Competitor Scraper — Microservice Plan

## 1. Goal & scope

Build a standalone microservice that ingests competitor content from Instagram
(per-username scans and per-hashtag scans), persists every relevant artefact
— **feed posts, reels, stories, highlights, comments, authors, hashtags** —
into the **shared main Postgres** used by `agent-platform`, and exposes a
small FastAPI control plane for queueing/monitoring scan jobs. The output is
the raw data layer that a later AI generation pipeline will consume to produce
"look-alike" content.

Two operating modes coexist:
- **Ad-hoc scans** — one-shot jobs queued via the API.
- **Tracked targets** — usernames or hashtags registered in `ig_scan_targets`
  that an internal scheduler re-scans on a fixed cadence (default daily). On
  the **first** scan of a target the worker performs a full backfill of all
  available posts; on subsequent scans it fetches only items newer than the
  last seen post (incremental). Stories are always re-scanned in full each
  run because they expire after 24h.

**Phasing.** Work is split into two phases (see § 13 for the milestone
breakdown):

- **Phase 1 — Core scraper.** Everything needed to reliably collect, store,
  score, and serve competitor content: scraping, anti-detection, jobs,
  scheduler, MCP, webhooks, hardening. Ends with a production-ready
  service whose REST + MCP API exposes scored, queryable data.
- **Phase 2 — AI substrate.** Caption embeddings (pgvector), LLM-derived
  structured features, similarity search, and the integration surface the
  future AI generator will use. Layered cleanly on top of Phase 1; nothing
  in Phase 2 changes Phase 1's data model — only adds tables.

This split exists so Phase 1 can ship and start producing useful data
while Phase 2 is still being designed/budgeted. The AI generation pipeline
can begin consuming Phase 1 data over MCP from day one and progressively
upgrade to Phase 2 features when they land.

Out of scope (both phases): the AI *generation* pipeline itself, media file
downloads (CDN URLs only), DM scraping, follower graph harvesting, paid
Graph API integration.

## 2. High-level architecture

```
                ┌───────────────────────────┐
                │   agent-platform (main)   │
                │   FastAPI + Postgres      │
                └────────────┬──────────────┘
                             │  (same Postgres,
                             │   ig_* schema/prefix)
                             ▼
   ┌──────────────────────────────────────────────┐
   │           ig-scraper service (NEW)           │
   │                                              │
   │  ┌──────────────┐    ┌──────────────────┐    │
   │  │ FastAPI API  │    │  Worker process  │    │
   │  │  (control)   │    │  (asyncio loop)  │    │
   │  └──────┬───────┘    └─────────┬────────┘    │
   │         │ enqueue/inspect      │ claim+run   │
   │         ▼                      ▼             │
   │      ┌────────────────────────────┐          │
   │      │  ig_scrape_jobs (Postgres) │          │
   │      └────────────────────────────┘          │
   │         │                                    │
   │         ▼                                    │
   │  ┌────────────────────────────────────────┐  │
   │  │  instagrapi client pool                │  │
   │  │  + proxy rotator + account rotator     │  │
   │  └────────────────────────────────────────┘  │
   │                  │                           │
   │                  ▼                           │
   │            Instagram (private API)           │
   └──────────────────────────────────────────────┘
```

Two long-lived processes share the same Docker image:
- **API process** (`uvicorn`) — exposes `/jobs`, `/accounts`, `/proxies`, `/health`.
- **Worker process** — runs an `asyncio` loop that claims `queued` jobs from
  the DB, executes them, and writes results.

Both use the same Postgres connection settings as `agent-platform`. All new
tables live in that database with an `ig_` prefix to avoid collisions.

## 3. Repository layout

New top-level service folder inside the existing repo (keeps deploy simple,
keeps the app code isolated):

```
agent-platform/
└── services/
    └── ig_scraper/
        ├── pyproject.toml          # own deps: fastapi, instagrapi, psycopg, sqlmodel
        ├── Dockerfile
        ├── README.md
        ├── alembic/                # migrations for ig_* tables
        ├── app/
        │   ├── main.py             # FastAPI entry
        │   ├── worker.py           # asyncio worker entry
        │   ├── core/
        │   │   ├── config.py       # reuses POSTGRES_* envs from main app
        │   │   ├── logging.py      # structlog (mirrors agent-platform)
        │   │   └── metrics.py      # prometheus
        │   ├── api/v1/
        │   │   ├── jobs.py
        │   │   ├── accounts.py
        │   │   ├── proxies.py
        │   │   └── health.py
        │   ├── models/             # SQLModel tables (ig_*)
        │   │   ├── account.py
        │   │   ├── proxy.py
        │   │   ├── job.py
        │   │   ├── target.py
        │   │   ├── post.py
        │   │   ├── comment.py
        │   │   ├── user.py         # IG user (not platform user)
        │   │   └── hashtag.py
        │   ├── services/
        │   │   ├── instagrapi_client.py   # session loader, login, settings
        │   │   ├── account_pool.py        # picks a healthy account
        │   │   ├── proxy_pool.py          # picks a healthy proxy
        │   │   ├── scraper_user.py        # username scrape flow
        │   │   ├── scraper_hashtag.py     # hashtag scrape flow
        │   │   ├── persistence.py         # upsert posts/comments
        │   │   └── filters.py             # min-likes/impressions gate
        │   └── schemas/                   # pydantic request/response
        └── tests/
```

`docker-compose.yml` in the repo root adds two services (`ig-scraper-api`,
`ig-scraper-worker`) pointing at the same image with different commands.

## 4. Database schema (additions to main Postgres)

All tables prefixed `ig_`. Migrations managed by Alembic inside the
`ig_scraper` service folder so the main app's SQLModel `metadata.create_all`
is unaffected.

### 4.1 Auth/infra tables

**`ig_accounts`** — credentials we control for scraping.

| column | type | notes |
| - | - | - |
| id | uuid PK | |
| username | text unique | |
| password_enc | bytea | encrypted with `IG_SECRET_KEY` (Fernet) |
| session_blob | jsonb null | instagrapi `dump_settings()` output, refreshed after login |
| status | text | `active`, `cooldown`, `challenge_required`, `banned`, `disabled` |
| proxy_id | uuid null FK → `ig_proxies.id` | sticky proxy per account |
| timezone | text default `'UTC'` | account's declared TZ, drives active hours |
| active_hours_start | smallint default 8 | local hour, inclusive |
| active_hours_end | smallint default 23 | local hour, exclusive |
| weekday_pattern | smallint default 127 | bitmap, Mon=1 ... Sun=64; 127 = all days |
| quota_tier | text default `'fresh'` | `fresh` / `mid` / `warm`, drives daily request cap |
| cooldown_until | timestamptz null | when set, pool skips this account until now() ≥ value |
| last_used_at | timestamptz | |
| last_login_at | timestamptz | |
| failure_count | int | resets on success |
| notes | text | free-form |
| created_at, updated_at | timestamptz | |

**`ig_proxies`** — generic HTTP/SOCKS5 pool.

| column | type | notes |
| - | - | - |
| id | uuid PK | |
| protocol | text | `http`, `https`, `socks5` |
| host | text | |
| port | int | |
| username | text null | |
| password_enc | bytea null | encrypted |
| label | text | provider tag, e.g. `brightdata-resi-dk` |
| status | text | `active`, `cooldown`, `dead` |
| last_ok_at | timestamptz | |
| failure_count | int | trips to `cooldown` past threshold |
| created_at, updated_at | timestamptz | |

### 4.2 Job table (the "simple job/task structure")

**`ig_scrape_jobs`** — single source of truth for queued/running/finished work.

| column | type | notes |
| - | - | - |
| id | uuid PK | |
| job_type | text | `user_feed_full`, `user_feed_incremental`, `user_stories`, `user_highlights`, `hashtag_top`, `hashtag_recent`, `user_enrich` |
| target | text | username or hashtag (without `#`/`@`) |
| scan_target_id | uuid null FK → `ig_scan_targets.id` | set when the job was auto-created by the scheduler; null for ad-hoc jobs |
| status | text | `queued`, `running`, `succeeded`, `failed`, `cancelled` |
| priority | int default 100 | lower = sooner |
| params | jsonb | filter rules, max\_posts, since\_date, fetch\_comments, comment\_limit |
| min_likes | int null | shortcut for filter |
| min_impressions | int null | only meaningful for views; falls back to play\_count |
| account_id | uuid null FK | which account picked it up |
| proxy_id | uuid null FK | which proxy was used |
| attempt | int default 0 | bumps on retry |
| max_attempts | int default 3 | |
| error | text null | last error message |
| stats | jsonb null | `{posts_seen, posts_saved, comments_saved, skipped_by_filter}` |
| scheduled_for | timestamptz | claimable when `now() >= scheduled_for` |
| started_at, finished_at | timestamptz null | |
| created_at | timestamptz | |

Worker claim query (atomic):

```sql
UPDATE ig_scrape_jobs
SET status = 'running', started_at = now(), attempt = attempt + 1
WHERE id = (
  SELECT id FROM ig_scrape_jobs
  WHERE status = 'queued' AND scheduled_for <= now()
  ORDER BY priority ASC, created_at ASC
  FOR UPDATE SKIP LOCKED
  LIMIT 1
)
RETURNING *;
```

`SKIP LOCKED` lets multiple workers run safely. No Redis required.

### 4.3 Content tables

**`ig_users`** — Instagram users observed during scraping (the *targets*, not
our scraping accounts).

| column | type | notes |
| - | - | - |
| id | bigint PK | IG numeric pk |
| username | text unique | |
| full_name | text | |
| biography | text | |
| follower_count | int | |
| following_count | int | |
| media_count | int | |
| is_business | bool | |
| is_verified | bool | |
| profile_pic_url | text | |
| raw | jsonb | full user payload from instagrapi for forensics |
| first_seen_at, last_seen_at | timestamptz | |

**`ig_hashtags`**

| column | type | notes |
| - | - | - |
| name | text PK | lowercased, no `#` |
| media_count | bigint | snapshot from last scan |
| last_scanned_at | timestamptz | |

**`ig_posts`** — one row per media item.

| column | type | notes |
| - | - | - |
| id | bigint PK | IG `pk` |
| code | text unique | shortcode (`/p/<code>/`) |
| media_type | smallint | 1 photo / 2 video / 8 carousel |
| product_type | text | `feed`, `clips`, `igtv` |
| author_id | bigint FK → `ig_users.id` | |
| caption | text | |
| taken_at | timestamptz | |
| like_count | int | |
| comment_count | int | |
| play_count | bigint null | reels/video views (proxy for impressions) |
| view_count | bigint null | when available |
| video_duration | float null | |
| thumbnail_url | text | |
| media_urls | jsonb | array of CDN URLs (image variants / video / carousel children) |
| location | jsonb null | |
| music_info | jsonb null | |
| hashtags | text[] | extracted from caption |
| mentions | text[] | extracted from caption |
| raw | jsonb | full media payload for later re-parsing |
| discovered_via_job_id | uuid FK → `ig_scrape_jobs.id` | |
| first_seen_at, last_seen_at | timestamptz | |

**`ig_post_hashtags`** — many-to-many for proper hashtag analytics.

| post_id | bigint FK | |
| hashtag | text FK → `ig_hashtags.name` | |

**`ig_comments`**

| column | type | notes |
| - | - | - |
| id | bigint PK | |
| post_id | bigint FK → `ig_posts.id` | |
| author_id | bigint FK → `ig_users.id` | |
| parent_comment_id | bigint null | for replies |
| text | text | |
| like_count | int | |
| created_at_ig | timestamptz | from IG |
| raw | jsonb | |

**`ig_stories`** — ephemeral 24h stories. Captured every daily run because
they vanish.

| column | type | notes |
| - | - | - |
| id | bigint PK | IG `pk` |
| author_id | bigint FK → `ig_users.id` | |
| media_type | smallint | 1 photo / 2 video |
| taken_at | timestamptz | from IG |
| expires_at | timestamptz | `taken_at + 24h` |
| video_duration | float null | |
| media_url | text | original CDN URL (will expire) |
| thumbnail_url | text | |
| caption | text null | rare on stories |
| mentions | text[] | sticker mentions |
| hashtags | text[] | sticker hashtags |
| link_sticker_url | text null | swipe-up / link sticker target |
| seen_count | int null | sometimes exposed |
| raw | jsonb | full payload |
| discovered_via_job_id | uuid FK | |
| captured_at | timestamptz | when our scraper saw it |

`UNIQUE(id)` is enough — stories are not re-fetchable once expired, so we
never update a row, just insert.

**`ig_highlights`** — saved story containers ("Highlights" on a profile).

| column | type | notes |
| - | - | - |
| id | bigint PK | IG `pk` |
| owner_id | bigint FK → `ig_users.id` | |
| title | text | |
| cover_url | text | |
| media_count | int | |
| raw | jsonb | |
| last_scanned_at | timestamptz | |

**`ig_highlight_items`** — many-to-many between highlights and the underlying
story media. Note: highlight items reuse `ig_stories.id` because they are the
same media objects that the user chose to preserve past 24h, so they remain
fetchable.

| highlight_id | bigint FK → `ig_highlights.id` | |
| story_id | bigint FK → `ig_stories.id` | |
| position | int | order within the highlight |

### 4.4 Tracked targets (the daily-scan registry)

**`ig_scan_targets`** — declares "this username/hashtag should be re-scanned
on a cadence". The scheduler reads this table once a minute and enqueues
jobs whose `next_run_at <= now()`.

| column | type | notes |
| - | - | - |
| id | uuid PK | |
| kind | text | `user` or `hashtag` |
| value | text | username or hashtag (lowercased) |
| status | text | `active`, `paused`, `pending_review` (auto-discovered, awaiting human OK) |
| interval_hours | int default 24 | re-scan cadence; 24 = daily |
| fetch_feed | bool default true | feed posts + reels |
| fetch_stories | bool default true | only meaningful for `kind=user` |
| fetch_highlights | bool default false | initial run only by default |
| fetch_comments | bool default true | |
| comment_limit | int default 50 | per post |
| min_likes | int null | filter applied during scan |
| min_impressions | int null | filter applied during scan |
| hashtag_section | text default `top` | `top` or `recent`, ignored for `kind=user` |
| first_backfill_done | bool default false | flips to true after `user_feed_full` succeeds |
| last_seen_post_id | bigint null | most recent IG `pk` we ingested for this target — used as the "stop pagination here" cursor for incremental scans |
| last_seen_taken_at | timestamptz null | |
| last_run_at | timestamptz null | |
| next_run_at | timestamptz | scheduler claim key |
| last_run_job_id | uuid null FK → `ig_scrape_jobs.id` | |
| auto_discovered | bool default false | true if created by hashtag enrichment |
| source_target_id | uuid null FK → `ig_scan_targets.id` | which hashtag target surfaced this user |
| created_at, updated_at | timestamptz | |

`UNIQUE(kind, value)` so we never double-track the same handle.

Indexes worth declaring up-front: `ig_posts(author_id, taken_at desc)`,
`ig_posts(like_count)`, `ig_posts(play_count)`, `ig_post_hashtags(hashtag)`,
`ig_comments(post_id)`, `ig_stories(author_id, taken_at desc)`,
`ig_scrape_jobs(status, scheduled_for)`,
`ig_scan_targets(status, next_run_at)`.

## 5. instagrapi integration

### 5.1 Login & session strategy

- Each `ig_accounts` row stores an encrypted session blob produced by
  `client.dump_settings()`. On worker start, the account pool calls
  `client.load_settings(blob)` and `client.login(username, password)` so
  instagrapi can re-use device fingerprint and avoid full re-login.
- After every successful job, persist refreshed settings back to the row
  (`session_blob`, `last_used_at`).
- On `ChallengeRequired` / `LoginRequired` / `BadPassword`: mark account
  `challenge_required` or `banned`, surface in `/accounts` API, do not retry
  silently.

### 5.2 Proxy binding

- Each account is sticky-bound to one proxy (`ig_accounts.proxy_id`). Switching
  proxy mid-session is the fastest way to trigger a challenge.
- Proxy URL passed to `client.set_proxy("http://user:pass@host:port")` before
  the first request.
- Health-check endpoint: a cheap `client.get_timeline_feed(amount=1)` after
  login. On failure, increment `failure_count`; trip to `cooldown` at 3 fails.

### 5.3 Anti-detection / throttling strategy

The cheapest way to get an account banned is to behave like a script. The
goal of this layer is to make traffic patterns indistinguishable from a
human using the official Instagram app on a phone. Every knob below is
configurable so we can dial it tighter if challenge rates climb.

**A. Per-action delay tiers, not a flat sleep**

Different IG endpoints have different "natural" cadences. A flat 2–6s
delay is a tell. We override `instagrapi`'s default with action-aware
ranges (lognormal-jittered, not uniform — uniform is also a tell):

| action | delay range (seconds) |
| - | - |
| feed page (load posts list) | 4 – 10 |
| open a post / fetch comments | 6 – 14 |
| user profile fetch | 5 – 12 |
| story tray | 3 – 8 |
| hashtag page | 7 – 15 |
| login / settings change | 20 – 40 |

Implementation: a `human_delay(action_kind)` helper that draws from a
lognormal distribution clipped to the range above, then sleeps. The mean
sits ~30% below the upper bound so most calls are slightly slow, with the
occasional "user got distracted" longer pause.

**B. Micro-jitter inside loops**

Between consecutive items inside a loop (e.g. fetching comments for
post #3 then #4) we add a small jitter (0.5–2.0s) on top of the action
delay. Without this, even varied per-action delays produce a suspiciously
regular cadence in aggregate.

**C. "Distracted human" macro-pauses**

Every 8–20 actions (random per session) the worker takes a long break of
30–180 seconds before continuing. Models the user putting the phone down.
Once per session (probability 1/3) it takes a 5–15 minute break.

**D. Session length & cooldown**

A single session ("a job run on one account") has a hard cap:
- ≤ 25 minutes of wall time, OR
- ≤ 300 instagrapi calls, OR
- ≤ 10 minutes of continuous API time without a macro-pause.

Whichever hits first ends the session. The account then enters
`cooldown` for 20–60 random minutes before it can be picked again. If a
target's job needs more than that, it's split across multiple sessions
(possibly across multiple accounts).

**E. Account active hours (timezone-aware)**

Each `ig_accounts` row stores `active_hours_start` and `active_hours_end`
(in the account's declared timezone) plus a `weekday_pattern` bitmap.
Default 08:00–23:00 local, all days. The pool refuses to pick an account
outside its active window. This kills the "scraper that runs 24/7"
signature — real users sleep.

**F. Per-account daily quotas (already in plan)**

`IG_MAX_REQUESTS_PER_ACCOUNT_PER_DAY` rolling 24h cap. New defaults:
- Warm accounts (used for ≥14 days, no challenges): 1500.
- Mid accounts (3–14 days, no challenges): 800.
- Fresh accounts (<3 days old to us): 250.

Stored as `quota_tier` on `ig_accounts`, recomputed nightly.

**G. Account warm-up**

A new account isn't allowed to run scrapes immediately. For its first
72h on the platform side, the worker does only "looks like a real user"
traffic on a low cadence: open the timeline, like an occasional post on
its own feed (toggleable), watch a couple of stories, then exit. After
72h the quota tier auto-promotes from `fresh` to `mid`.

**H. Sticky everything**

One account is bound to one proxy (`ig_accounts.proxy_id`) for its entire
lifetime. instagrapi `device_settings`, `user_agent`, `locale`, and
`timezone_offset` are generated once at account creation and stored in
`ig_accounts.session_blob` — never regenerated. Switching device or proxy
mid-life is the fastest path to a challenge.

**I. Adaptive backoff on signals**

The worker watches for soft signals from instagrapi and reacts before a
hard ban:

| signal | reaction |
| - | - |
| `PleaseWaitFewMinutes` | end session immediately, account → `cooldown` for 2–4h, halve its `quota_tier` for 24h |
| HTTP 429 | end session, cooldown 1–2h |
| `ChallengeRequired` | account → `challenge_required` (humans handle), proxy stays bound |
| `feedback_required` (action-blocked) | end session, cooldown 12–24h, alert |
| 3 consecutive empty/garbage responses | proxy probably burnt — proxy → `cooldown`, end session |

Reactions are persisted so the next scheduler tick honours them.

**J. Spread the daily fleet**

When the scheduler enqueues the day's tracked-target jobs, it does **not**
fire them at the cadence boundary (e.g. midnight UTC). It computes
`next_run_at` per target as
`previous_run_at + interval_hours ± 15% jitter`, and on the very first
schedule of a target it picks a random offset within the interval. Result:
the daily fleet is smeared across the day instead of bursting at one
clock minute.

**K. One account → one job at a time**

`account_pool.acquire()` uses `SELECT ... FOR UPDATE SKIP LOCKED` so two
workers can't grab the same account. Combined with E and I, this means
each account's traffic stream looks like one person scrolling at human
speed, not a machine.

**Configurable, observable**

All numeric ranges above are env-var driven (`IG_DELAY_FEED_MIN`,
`IG_DELAY_FEED_MAX`, `IG_MACRO_PAUSE_EVERY_MIN`, ...). Prometheus
counters surface every reaction in I so we can tell at a glance whether
the strategy is working: rising `ig_account_failures_total{reason="challenge"}`
is the signal to widen delays or shrink quotas.

## 6. Scraping flows

The user scan is split by job type so the worker can pick the right
instagrapi endpoint and the right termination rule. Reels and feed posts
share `ig_posts` because they have the same shape; stories live in
`ig_stories`.

### 6.1 `user_feed_full` — first-time backfill

Used the **first** time a user target is scanned. Pulls everything Instagram
will give us.

Inputs: `target` (username), filters (`min_likes`, `min_impressions`,
`params.since`), `params.fetch_comments`, `params.comment_limit`.

Steps:
1. Resolve username → `user_id` via `client.user_id_from_username_v1`.
2. Upsert `ig_users` from `client.user_info_v1(user_id)` (full profile incl.
   `follower_count`, `following_count`, `media_count`, biography).
3. Iterate `client.user_medias_paginated(user_id, amount=0)` until
   pagination ends or the hard cap `IG_MAX_POSTS_PER_JOB` is hit. `amount=0`
   means "all available".
4. Also iterate `client.user_clips_paginated(user_id, amount=0)` and merge
   into the same set keyed by media `pk`. Some accounts post reels-only
   that don't surface in `user_medias`.
5. Filter (§ 7) → upsert `ig_posts` → upsert hashtags/mentions → optionally
   fetch and upsert comments.
6. On success, the job result writes back to `ig_scan_targets`:
   `first_backfill_done = true`,
   `last_seen_post_id = max(post.id)`,
   `last_seen_taken_at = max(post.taken_at)`,
   `next_run_at = now() + interval_hours`.

### 6.2 `user_feed_incremental` — daily delta

Default for the daily scheduler once a target has its backfill flag set.

Steps:
1. Read `last_seen_post_id` / `last_seen_taken_at` from `ig_scan_targets`.
2. Page `client.user_medias_paginated(user_id, amount=200)` (most recent
   first). Stop as soon as we encounter a post whose `pk == last_seen_post_id`
   or whose `taken_at <= last_seen_taken_at`. Stash the new ones.
3. Same merge with `user_clips_paginated` capped to e.g. 50 most recent reels
   for the same stop condition.
4. Filter + upsert as in 6.1. Existing posts that resurface get their
   counters updated (engagement growth tracking is a free side-effect).
5. Comments are fetched **only for newly-seen posts**, not for ones we
   already know about, to keep the daily run cheap.
6. Update target cursors and `next_run_at`.

### 6.3 `user_stories` — daily ephemeral capture

Stories must be scraped at least every 24h or they're gone. The scheduler
emits this job once per day per active target with `fetch_stories=true`.

Steps:
1. `client.user_stories_v1(user_id)` → list of currently live stories.
2. Insert each into `ig_stories` (no upsert needed — story IDs are unique
   and we only see each one once before it expires).
3. Extract sticker mentions (`@user`) and hashtags (`#tag`). Mentions can
   feed the auto-enrichment pipeline (§ 6.6).
4. Stats counter `ig_stories_saved_total` for monitoring.

### 6.4 `user_highlights` — opt-in saved stories

By default fetched once when a target is first added (or manually triggered),
not on every daily run. Stories saved to highlights remain fetchable
indefinitely.

Steps:
1. `client.user_highlights(user_id)` → list of highlight reels.
2. For each highlight: `client.highlight_info(highlight_pk)` → underlying
   story media.
3. Upsert `ig_highlights`, then `ig_stories` for each item, then
   `ig_highlight_items` to wire them together.

### 6.5 `hashtag_top` / `hashtag_recent`

Inputs: `target` (hashtag), `params.max_posts` (default 100), filter knobs,
`params.auto_enrich_users` (bool, default true),
`params.min_followers_for_enrich` (default 5000),
`params.min_media_for_enrich` (default 12).

Steps:
1. Upsert `ig_hashtags`.
2. `client.hashtag_medias_top_v1(name, amount=max_posts)` or
   `hashtag_medias_recent_v1`.
3. For each media:
   - Apply post filter (§ 7) → upsert `ig_posts`.
   - Run the **author enrichment** subroutine (§ 6.6).

### 6.6 Author enrichment from hashtag scans

When a hashtag scan surfaces a post by an author we don't yet know much
about — or have stale stats for — we want to (a) fill in their full profile
and (b) decide whether to start tracking them daily.

Subroutine, run for each unique author seen in a hashtag scan:

1. If the author already has an `ig_scan_targets` row, skip (we'll catch
   them on the next daily run).
2. If `ig_users.last_seen_at` is null or older than 7 days, call
   `client.user_info_v1(user_id)` to refresh `follower_count`,
   `following_count`, `media_count`, `is_business`, biography. Costs one
   extra API call per new author — gated by the daily quota.
3. Promotion check: if
   - `follower_count >= min_followers_for_enrich`, and
   - `media_count >= min_media_for_enrich`, and
   - `is_private == false`,

   then auto-create an `ig_scan_targets` row with:
   - `kind = 'user'`,
   - `status = 'pending_review'` (or `'active'` if
     `IG_AUTO_PROMOTE_DISCOVERED = true`),
   - `auto_discovered = true`,
   - `source_target_id =` the hashtag target's id,
   - `next_run_at = now()` (so the first full backfill kicks off on the
     next scheduler tick).
4. Otherwise just keep the user in `ig_users` with refreshed stats — useful
   data for analytics, no daily commitment.

The `pending_review` status is the safety valve so we don't accidentally
flood the daily-scan budget with low-signal accounts. A human approves via
`POST /targets/{id}/activate` (or auto-promote if you trust the threshold).

### 6.7 Re-scan / freshness semantics

`ig_posts` is upserted on `id`, so re-scans update `like_count`,
`comment_count`, `play_count`, `last_seen_at`. This gives us engagement
growth over time for free — useful later for "what content went viral after
posting" analytics.

`ig_stories` is **insert-only**: a given story is observed once during its
24h window and never updated. Missing a daily run = permanently lost
stories for that day.

## 7. Filtering ("ignore below threshold")

Implemented in `services/filters.py`, applied **before** the comment fetch (so
we don't burn requests on posts we'll discard).

A post passes iff:
- `min_likes` is null OR `media.like_count >= min_likes`, AND
- `min_impressions` is null OR `coalesce(media.play_count, media.view_count, 0) >= min_impressions`, AND
- `params.since` is null OR `media.taken_at >= params.since`.

`min_impressions` is documented as "best-effort": Instagram only exposes
play\_count for video/reels, so for image posts the field is treated as 0 and
will be skipped if a threshold is set. The `/jobs` API returns a warning if a
caller sets `min_impressions` on what looks like a photo-only target.

## 8. API surface (FastAPI)

Prefix `/api/v1`, JSON. Auth is a single static API key checked by a FastAPI
dependency:

```python
async def require_api_key(x_api_key: str = Header(...)):
    if x_api_key != settings.IG_SCRAPER_API_KEY:
        raise HTTPException(401, "invalid api key")
```

No JWT, no user table. The scraper is an internal service called by your
backend / scheduler / dashboards. If a richer scheme is ever needed later
(per-caller keys with revocation), it slots in behind the same dependency
without touching the rest of the code. `slowapi` rate limiting reused from
`agent-platform` patterns.

### Jobs

- `POST /jobs` — enqueue an ad-hoc scan. Body: `{job_type, target, params,
  min_likes, min_impressions, priority, scheduled_for}`. Returns the created
  job.
- `GET /jobs?status=&job_type=&target=&limit=` — list jobs.
- `GET /jobs/{id}` — job detail incl. stats + last error.
- `POST /jobs/{id}/cancel` — sets `status=cancelled` if still queued; running
  jobs are flagged for cooperative cancellation (worker checks per page).
- `POST /jobs/{id}/retry` — clones a failed job back to `queued`.

### Tracked targets (the daily-scan registry)

- `POST /targets` — register a username or hashtag for recurring scans.
  Body: `{kind, value, interval_hours, fetch_stories, fetch_highlights,
  fetch_comments, comment_limit, min_likes, min_impressions,
  hashtag_section}`. The scheduler picks it up on the next tick; the very
  first run will be a `user_feed_full` (full backfill).
- `GET /targets?kind=&status=&auto_discovered=` — list, filterable.
- `GET /targets/{id}` — detail incl. cursors and last run summary.
- `PATCH /targets/{id}` — change cadence, filters, or pause it.
- `POST /targets/{id}/activate` — flip `pending_review` → `active` (used to
  approve auto-discovered users from hashtag enrichment).
- `POST /targets/{id}/run-now` — enqueue an immediate scan without waiting
  for `next_run_at`.
- `DELETE /targets/{id}` — soft-delete (status = `paused`); content stays
  in `ig_posts` etc.

### Accounts & proxies

- `GET /accounts` / `POST /accounts` / `POST /accounts/{id}/disable` —
  manage scraping accounts. POST takes plaintext password, server encrypts.
- `GET /proxies` / `POST /proxies` / `POST /proxies/{id}/test`.

### Read-only content

- `GET /users/{username}` — last known profile snapshot.
- `GET /posts?author=&hashtag=&min_likes=&since=` — recent posts.
- `GET /posts/{id}/comments` — comments for a post.
- `GET /stories?author=&since=` — recent stories.

### Ops

- `GET /health` — DB ok, worker heartbeat fresh, scheduler heartbeat fresh,
  ≥1 active account, ≥1 active proxy.

OpenAPI docs published at `/docs` like the main app.

## 8b. Scheduler

A third lightweight process inside the same image (`python -m app.scheduler`)
ticks once a minute and turns due `ig_scan_targets` rows into queued jobs:

```sql
-- find all active targets whose next_run_at has passed
SELECT * FROM ig_scan_targets
WHERE status = 'active' AND next_run_at <= now()
ORDER BY next_run_at ASC
FOR UPDATE SKIP LOCKED
LIMIT 100;
```

For each row, the scheduler decides which job(s) to enqueue:

| target state | jobs enqueued |
| - | - |
| `kind=user`, `first_backfill_done=false` | `user_feed_full` + (if `fetch_stories`) `user_stories` + (if `fetch_highlights`) `user_highlights` |
| `kind=user`, `first_backfill_done=true` | `user_feed_incremental` + (if `fetch_stories`) `user_stories` |
| `kind=hashtag` | `hashtag_top` and/or `hashtag_recent` |

After enqueueing, scheduler bumps `next_run_at = now() + interval_hours`
optimistically. If the resulting jobs all fail, the next run still happens
on schedule (we don't punish targets for transient failures).

Why not `pg_cron` or system cron? Keeping the scheduler in-process means
zero new infra and lets us read/write the same `ig_scan_targets` cursors
the worker is updating, in the same transaction boundary.

## 8c. MCP server interface (alongside REST)

### Why
The whole point of scraping competitor content is to feed an AI generation
pipeline. MCP is the standard surface AI agents (Claude, langgraph
workflows, the future generator that lives inside `agent-platform`) use to
read external data and trigger external work. Building MCP into v1 means
the generator can plug in with zero glue code; bolting it on later forces
us to duplicate validation, auth, and rate limiting.

### Transport
- **Streamable HTTP** (the current MCP standard) mounted on the same
  FastAPI app at `/mcp`. One container, two protocols (REST at `/api/v1`,
  MCP at `/mcp`). Implementation via the official Python `mcp` SDK / its
  `FastMCP` helper, mounted as an ASGI sub-app.
- A stdio mode (`python -m app.mcp_stdio`) is kept available for local
  development so a developer can attach Claude Desktop / Claude Code
  directly to their dev DB without going through HTTP.

### Auth
Same `IG_SCRAPER_API_KEY`, same dependency, different header convention:
MCP clients send `Authorization: Bearer <key>`. The MCP middleware checks
it before any tool handler runs. Stdio mode skips auth (local-only by
construction).

### Tools exposed (curated — not 1:1 with REST)

**Read tools** (safe, idempotent — what an agent will mostly use):
- `search_posts(author?, hashtag?, min_likes?, min_play_count?, since?, limit?)`
  — query `ig_posts` for inspiration / few-shot examples.
- `get_user_profile(username)` — last known `ig_users` snapshot incl.
  follower / following / media counts.
- `get_user_top_posts(username, by, since?, limit?)` — best-performing
  recent posts (`by` ∈ `likes`, `play_count`, `comments`).
- `get_recent_stories(username, since?)` — for accounts whose stories we
  capture.
- `get_post_comments(post_id, limit?)` — sample comments for tone analysis.
- `list_tracked_targets(kind?, status?)` — what we're currently watching.
- `get_job_status(job_id)` — for agents that just triggered a scan.

**Write tools** (require API key, write to job queue / target registry):
- `add_tracked_target(kind, value, interval_hours?, fetch_stories?,
  fetch_highlights?, min_likes?, min_impressions?)` — register a daily-scan
  target. The first scheduler tick will full-backfill it.
- `enqueue_user_scan(username, full_backfill=false, fetch_comments=true,
  comment_limit=50, min_likes?, min_impressions?)` — fire an ad-hoc scan,
  returns the job id.
- `enqueue_hashtag_scan(hashtag, section="top"|"recent",
  auto_enrich_users=true, min_likes?, min_impressions?, max_posts?)` —
  same for hashtags.
- `pause_target(target_id)` / `activate_target(target_id)` — lifecycle
  control, including approving auto-discovered users from hashtag
  enrichment.

**Resources** (long structured payloads MCP fetches lazily, not as tool
results):
- `ig://user/{username}/profile` — full latest profile JSON.
- `ig://user/{username}/posts/recent` — recent posts as a single document.
- `ig://post/{post_id}` — full post + comments.
- `ig://hashtag/{name}/top` — recent top posts under a hashtag.

**Deliberately NOT exposed via MCP** (security-sensitive, ops-only):
- Account CRUD (`ig_accounts`).
- Proxy CRUD (`ig_proxies`).
- Raw passthrough to instagrapi (would let an agent bypass our cache,
  filters, and quota accounting).

These stay REST-only behind the API key so an AI agent that gets prompt-
injected by scraped content can't, say, delete a scraping account.

### Code shape
Tool handlers are thin wrappers over the existing service-layer functions.
Same Pydantic schemas back both REST endpoints and MCP tools, so validation
isn't duplicated.

```python
# services/ig_scraper/app/mcp_server.py
from mcp.server.fastmcp import FastMCP
from app.services import scraper_user, persistence, targets

mcp = FastMCP("ig-scraper")

@mcp.tool()
async def search_posts(author: str | None = None,
                       hashtag: str | None = None,
                       min_likes: int | None = None,
                       since: str | None = None,
                       limit: int = 20) -> list[dict]:
    """Search scraped posts by author/hashtag/engagement."""
    return await persistence.search_posts(...)

@mcp.tool()
async def enqueue_user_scan(username: str,
                            full_backfill: bool = False,
                            fetch_comments: bool = True,
                            min_likes: int | None = None) -> dict:
    """Queue a scan of an Instagram username."""
    job = await targets.enqueue_user_scan(...)
    return {"job_id": str(job.id), "status": job.status}

# in app/main.py:
app.mount("/mcp", mcp.streamable_http_app())
```

### Cost
Roughly a day of work and ~150 lines of code (LOC) on top of the REST
service. Breakdown:

- `app/mcp_server.py` — tool and resource handlers, ~100 lines. Each tool
  is a thin wrapper (5–10 lines) over an existing service-layer function
  plus a docstring the MCP SDK uses as the tool's description.
- Auth shim — 10–20 lines: middleware that checks the
  `Authorization: Bearer` header against `IG_SCRAPER_API_KEY` before any
  tool handler runs.
- Mount line in `app/main.py` plus the stdio entry point at
  `app/mcp_stdio.py` — 20–30 lines combined.
- One line in `pyproject.toml` adding the `mcp` package.

The estimate is small because the work is already done elsewhere:
persistence helpers, Pydantic schemas, and the job/target services exist
for the REST API, and MCP handlers just call them. No new infra, no new
container.

### Milestone slot
Inserted as **M5.5 — MCP surface (1 day)** between feed scrape (M4) and
hashtag enrichment (M6). Read tools come first (they only need the
persistence layer, no scheduler dependency). Write tools land alongside or
just after the scheduler in M7.

## 9. Worker loop

```python
async def worker_loop():
    while not shutdown.is_set():
        job = await claim_next_job()           # SKIP LOCKED query
        if job is None:
            await asyncio.sleep(2)
            continue
        try:
            account = await account_pool.acquire(job)
            client = await instagrapi_client.for_account(account)
            if job.job_type == "user_scan":
                stats = await scraper_user.run(client, job)
            elif job.job_type == "hashtag_scan":
                stats = await scraper_hashtag.run(client, job)
            await mark_succeeded(job, stats)
        except RetryableError as e:
            await mark_retry(job, e)            # back to queued with backoff
        except FatalError as e:
            await mark_failed(job, e)
        finally:
            await account_pool.release(account)
```

Concurrency knob: `IG_WORKER_CONCURRENCY` (default 2) spawns N copies of the
loop in the same process; each grabs its own account.

Heartbeat: worker writes `now()` to a `ig_worker_heartbeat` table every 10s;
`/health` rejects stale heartbeats (>60s).

## 10. Configuration (env vars)

Reuses the existing `POSTGRES_*` settings from `agent-platform`. New ones:

- `IG_SCRAPER_API_KEY` — static API key for the FastAPI control plane.
- `IG_SECRET_KEY` — Fernet key for password & proxy creds encryption.
- `IG_WORKER_CONCURRENCY` — default `2`.
- `IG_SCHEDULER_TICK_SECONDS` — default `60`.
- Per-action delay ranges (lognormal, see § 5.3 A): `IG_DELAY_FEED_MIN`/`MAX`
  (4/10), `IG_DELAY_POST_MIN`/`MAX` (6/14), `IG_DELAY_PROFILE_MIN`/`MAX`
  (5/12), `IG_DELAY_STORY_MIN`/`MAX` (3/8), `IG_DELAY_HASHTAG_MIN`/`MAX`
  (7/15), `IG_DELAY_LOGIN_MIN`/`MAX` (20/40).
- `IG_MICRO_JITTER_MIN`/`MAX` — default `0.5` / `2.0`.
- Macro-pause knobs (§ 5.3 C): `IG_MACRO_PAUSE_EVERY_MIN`/`MAX` (8/20),
  `IG_MACRO_PAUSE_SECONDS_MIN`/`MAX` (30/180),
  `IG_LONG_BREAK_PROBABILITY` (0.33),
  `IG_LONG_BREAK_SECONDS_MIN`/`MAX` (300/900).
- Session caps (§ 5.3 D): `IG_SESSION_MAX_MINUTES` (25),
  `IG_SESSION_MAX_CALLS` (300),
  `IG_ACCOUNT_COOLDOWN_MIN`/`MAX` (1200/3600 seconds).
- Daily quotas by tier (§ 5.3 F):
  `IG_DAILY_QUOTA_FRESH` (250), `IG_DAILY_QUOTA_MID` (800),
  `IG_DAILY_QUOTA_WARM` (1500).
- `IG_WARMUP_HOURS` — default `72`.
- `IG_TARGET_INTERVAL_JITTER_PCT` — default `15`.
- (legacy, kept for back-compat) `IG_MAX_REQUESTS_PER_ACCOUNT_PER_DAY` —
  used as a hard ceiling above the tier-specific quotas; default `1500`.
- `IG_MAX_POSTS_PER_JOB` — default `2000` hard cap (raised because
  `user_feed_full` may need to walk a lot of history on first scan).
- `IG_COMMENT_DEFAULT_LIMIT` — default `50`.
- `IG_DEFAULT_INTERVAL_HOURS` — default `24` for new tracked targets.
- `IG_AUTO_PROMOTE_DISCOVERED` — default `false`. When true, hashtag
  enrichment skips `pending_review` and activates discovered users
  immediately.
- `IG_MIN_FOLLOWERS_FOR_ENRICH` — default `5000`.
- `IG_MIN_MEDIA_FOR_ENRICH` — default `12`.

## 11. Observability

- `structlog` JSON logs with `job_id`, `account_id`, `proxy_id`, `target` in
  context — same pattern as `agent-platform`.
- Prometheus counters: `ig_jobs_total{type,status}`,
  `ig_posts_saved_total{job_type}`, `ig_comments_saved_total`,
  `ig_account_failures_total{reason}`, `ig_proxy_failures_total`.
- Histograms: `ig_job_duration_seconds`, `ig_post_fetch_seconds`.
- Existing Grafana folder gets a new dashboard JSON committed at
  `grafana/dashboards/ig_scraper.json`.

## 12. Deployment

`docker-compose.yml` additions:

```yaml
  ig-scraper-api:
    build: ./services/ig_scraper
    command: uvicorn app.main:app --host 0.0.0.0 --port 8081
    env_file: .env
    ports: ["8081:8081"]
    depends_on: [postgres]

  ig-scraper-worker:
    build: ./services/ig_scraper
    command: python -m app.worker
    env_file: .env
    depends_on: [postgres]
    deploy:
      replicas: 1   # scale horizontally as account pool grows

  ig-scraper-scheduler:
    build: ./services/ig_scraper
    command: python -m app.scheduler
    env_file: .env
    depends_on: [postgres]
    deploy:
      replicas: 1   # exactly one — the SKIP LOCKED claim makes this safe
                    # if you accidentally run more, but only one is needed
```

Same image, three commands. Health checks: `GET /health` for the API, two
heartbeat rows (`worker`, `scheduler`) tracked in `ig_worker_heartbeat`
(extend the column with a `process` field).

## 13. Phased rollout

### Phase 1 — Core scraper (≈ 12–15 dev-days)

End state: a production-ready microservice that scrapes Instagram on a
daily cadence with anti-detection hardening, scores every post,
dispatches webhooks on high-quality content, and exposes the data over
REST + MCP. Phase 1 contains **no embedding work and no LLM calls** —
all scoring is deterministic engagement math.

**M1 — Foundations (1–2 days)**
Repo skeleton, Alembic migrations for all Phase-1 `ig_*` tables (incl.
`ig_post_metric_snapshots`, `ig_audio_tracks`, `ig_webhooks`,
`ig_usage_daily`), Postgres read-replica DSN env var wired, Dockerfile,
compose wiring, structlog/prometheus glue, `/health`. No scraping yet.

**M2 — Account & proxy management (1 day)**
`ig_accounts` / `ig_proxies` CRUD endpoints, Fernet encryption, login flow
that populates `session_blob`, proxy test endpoint, `role='canary'` flag
honoured by the pool.

**M3 — Job queue + worker (1–2 days)**
`ig_scrape_jobs` table, `SKIP LOCKED` claim, worker loop with heartbeat,
job cancellation, `ig_usage_daily` counters incremented per call. Stub
scraper that just sleeps so we can prove the queue works under load.

**M4 — Feed scrape flows (2 days)**
`user_feed_full` and `user_feed_incremental` with cursor management on
`ig_scan_targets`. Post + comment upsert with caption feature extraction
(language, emoji/hashtag/mention counts, has_question, has_cta) and
caption simhash for duplicate detection. `ig_post_metric_snapshots` row
written on every post upsert. Audio normalised into `ig_audio_tracks`
when present. End-to-end test against a throwaway IG account hitting a
known target, running it twice to verify the incremental cursor and that
metric snapshots accumulate.

**M5 — Stories & highlights (1 day)**
`user_stories` (insert-only, ephemeral) and `user_highlights` (opt-in).
`ig_stories` table populated.

**M5.5 — MCP read surface (1 day)**
Read-only MCP tools wired up (see § 8c): `search_posts`,
`get_user_profile`, `get_user_top_posts`, `get_recent_stories`,
`get_post_comments`, `list_tracked_targets`, `get_job_status`. Write
tools follow in M7. Resources for `ig://user/...`, `ig://post/...`,
`ig://hashtag/...`.

**M6 — Hashtag scan + author enrichment (1–2 days)**
`hashtag_top` / `hashtag_recent` reusing the same post pipeline. The
enrichment subroutine that calls `user_info_v1` for unknown authors and
auto-creates `pending_review` targets above the follower/media thresholds.
The median-score gate is wired but inert until M8 fills scores.

**M7 — Scheduler & tracked targets (1 day)**
`/targets` CRUD, scheduler tick loop that turns due `ig_scan_targets` into
queued jobs (full vs incremental decision based on `first_backfill_done`).
Heartbeat for the scheduler. Canary scheduled job (one tracked target,
hourly cadence, dedicated account). MCP write tools added
(`add_tracked_target`, `enqueue_user_scan`, `enqueue_hashtag_scan`,
`pause_target`, `activate_target`).

**M8 — Scoring & analytical views (1.5–2 days)**
Score computation function (§ 14b) wired into the post upsert path and
into a nightly recompute job over the last 30 days. Materialised views:
`ig_top_posts_by_author` (hourly refresh), `ig_author_posting_pattern`
(daily), `ig_hashtag_velocity` (daily). REST `min_score`/`order=score_desc`
filters. MCP `get_high_scoring_posts` tool. Hashtag-enrichment
median-score gate activated.

**M9 — Webhooks & retention (1 day)**
`ig_webhooks` table + dispatcher with HMAC signature, retry, backoff.
Triggers on score-threshold crossings and tracked-target completions.
GDPR `expires_at` columns added on `ig_comments.text` and
`ig_users.biography`; nightly nullifier job present but disabled by
default.

**M10 — Hardening & launch (1–2 days)**
Full anti-detection layer (§ 5.3) live: per-action delay tiers,
micro-jitter, macro-pauses, session caps, active hours, tiered quotas,
warm-up, adaptive backoff matrix for `PleaseWaitFewMinutes`,
`ChallengeRequired`, `feedback_required`, proxy failures. Grafana
dashboard (score distribution, account/proxy health, usage, challenge
rate). Runbook in `docs/`. **End of Phase 1.**

At this point the AI generation pipeline can already consume
Phase 1 data over MCP using `search_posts`, `get_user_top_posts`, and
`get_high_scoring_posts`. Anything semantic or LLM-derived comes later.

### Phase 2 — AI substrate (≈ 4–5 dev-days, schedule independently)

Layered on top of Phase 1 without touching its data model. Each
milestone here adds tables/columns; nothing in Phase 1 needs to change
to consume them.

**M11 — pgvector & caption embeddings (1.5 days)**
`CREATE EXTENSION pgvector` migration. New `ig_post_embeddings(post_id,
model, embedding vector, embedded_at)` table. New job type
`embed_post_batch` that pulls unembedded posts in chunks, calls the
embedding provider (default OpenAI `text-embedding-3-small`,
configurable to Voyage / Cohere multilingual for TR-heavy content),
writes vectors. Nightly enqueue of new posts. Cost recorded in
`ig_usage_daily`. New MCP tool `find_similar_posts(text, limit,
min_score?)` and REST `GET /posts/similar`.

**M12 — LLM-derived structured features (1.5 days)**
New `ig_post_llm_features(post_id, model, features jsonb,
classified_at)` table. Per post, a single LLM call extracts a small
JSON: `{tone, content_pillar, hook_type, cta_type, audience, themes[]}`.
Done lazily by an `extract_llm_features_batch` job. Stored alongside
embeddings, queryable via REST/MCP. Combines with embedding similarity
for richer filtering (`find_similar_posts(text, content_pillar="launch")`).

**M13 — Author style summaries & re-ranking (1 day)**
A weekly batch job calls the LLM with each tracked author's last 30
posts and stores a short style profile in
`ig_author_style(author_id, summary, dominant_pillars,
voice_attributes, refreshed_at)`. New MCP tool
`get_author_style(username)`. Optional MCP tool
`rerank_candidates(query_text, post_ids[])` that takes a coarse
embedding-search candidate set and returns LLM-reranked top-N — the
hybrid pattern from the conversation. **End of Phase 2.**

**M14 — Hand-off to AI generation (later, separate workstream)**
Lives in `agent-platform`'s langgraph layer, not in this microservice.
Consumes Phase 2 surfaces (`find_similar_posts`, `get_author_style`,
`rerank_candidates`) plus Phase 1 data to produce new content. Out of
scope for this plan; mentioned only so the Phase 2 API surface stays
designed for it.

## 14. Risks & mitigations

| Risk | Mitigation |
| - | - |
| instagrapi breaks when IG changes private API | Pin a tested version, alert on `ig_account_failures_total{reason="parse_error"}`, have a runbook to bump and re-test against a canary account. |
| Account bans cascade | Sticky proxy per account, daily quotas, rotation, surface `status` in API so humans can swap them out. |
| `min_impressions` is meaningless on photos | Documented in API; warning returned when threshold set against a photo-only target. |
| CDN URLs in `media_urls` expire | Acceptable for v1 (metadata-only). When AI gen needs originals we'll add an `ig_media_assets` table + S3 downloader as a separate job type. |
| Shared Postgres becomes a hotspot | All scraper writes go through batched upserts; `ig_*` tables have their own indexes; can be moved to a logical schema (`SET search_path`) if isolation needed later. |
| Secrets leakage | Passwords + proxy creds encrypted at rest with `IG_SECRET_KEY`; never logged; redact filter in structlog config. |

## 14b. Content scoring

The whole point of collecting this data is to surface "good content" to the
AI generation pipeline — so a post-level score that ranks competitor content
by quality/virality is core, not optional. This section defines that score
and the data needed to compute it.

### What we score

Every post (including reels) gets a `score` in `[0, 100]` recomputed on
every scan. Stories are intentionally **not** scored — they're ephemeral
and IG exposes few engagement signals for them.

### Components

The score is a weighted sum of normalised sub-scores, each in `[0, 1]`:

| component | meaning | how it's computed |
| - | - | - |
| `engagement_rate` | engagement relative to audience size | `(like_count + comment_count) / max(follower_count_at_post_time, 1)`, clipped to a sensible upper bound (e.g. 0.5) and rescaled. |
| `velocity` | speed of early engagement | likes-per-hour over the first ~24h after `taken_at`, derived from `ig_post_metric_snapshots`. Strongest predictor of virality. |
| `view_efficiency` | engagement per view (reels/video only) | `(like_count + comment_count) / max(play_count, 1)`, otherwise `null`. Catches reels that get pushed to lots of feeds but don't actually resonate. |
| `comment_intensity` | conversational pull | `comment_count / max(like_count, 1)`, capped. High ratio = controversial / discussion-driving. |
| `author_relative` | how this post compares to the author's own median | z-score of `engagement_rate` against the author's last 30 posts. Catches "this was a banger for THIS account specifically", which is the signal you actually want for style cloning. |
| `freshness` | time decay | `exp(-age_days / IG_SCORE_HALFLIFE_DAYS)`, default half-life 14 days. Keeps recent content surfacing without letting old content disappear entirely. |

Weights default to:
`engagement_rate 0.20, velocity 0.25, view_efficiency 0.10,
comment_intensity 0.10, author_relative 0.25, freshness 0.10`.

All weights and clip thresholds live in env vars (`IG_SCORE_W_*`,
`IG_SCORE_CLIP_*`) so we can re-tune without a deploy.

### Persistence

Two changes to the schema:

**Extend `ig_posts`** with:
- `score` numeric(5,2) — final score `[0, 100]`.
- `score_components` jsonb — sub-scores, for debugging/A-B-ing.
- `score_computed_at` timestamptz.

**New `ig_post_metric_snapshots`** — append-only time series, one row per
scan. Required for `velocity` and for tracking engagement curves over
time.

| post_id | bigint FK | |
| scanned_at | timestamptz | |
| like_count | int | |
| comment_count | int | |
| play_count | bigint null | |
| view_count | bigint null | |
| save_count | bigint null | when exposed |
| score | numeric(5,2) | snapshot of the score at this moment |
| (PK: `post_id, scanned_at`) | | |

Index `ig_post_metric_snapshots(post_id, scanned_at desc)`.

### When the score is computed

- Inline at ingest, on every post upsert (uses whatever metric snapshot
  history exists).
- Re-computed on every metric snapshot (cheap — just the components for
  one row).
- A nightly batch recomputes scores for all posts created in the last 30
  days, so weight changes propagate without a full table sweep.

### How callers use it

- REST: `GET /posts?author=...&min_score=70&order=score_desc`.
- MCP: a new read tool `get_high_scoring_posts(author?, hashtag?, since?,
  limit?, min_score=60)` — this is the primary tool the AI generator will
  call to gather "good examples".
- Per-author breakouts: a materialised view `ig_top_posts_by_author` that
  refreshes hourly, returning each author's top-N by score, used as
  "show me what's working for competitor X right now".

### Integration with hashtag enrichment

The auto-promotion threshold in § 6.6 currently uses follower count + media
count. Once scoring exists we add a third lever: a candidate user gets
auto-promoted only if their **median post score** over the last N posts
clears `IG_MIN_SCORE_FOR_ENRICH` (default 50). This filters out big
accounts that post low-quality content from polluting the daily-scan
budget — the whole point is to track accounts that produce content worth
imitating, not just popular ones.

## 14c. Additional capabilities — folded into milestones

These were originally scoped as "worth adding later" but have been folded
into the milestone plan (§ 13) because most are cheap and several affect
schema decisions that are painful to retrofit. The list below is a
reference for *what* each one does and *why* — milestone assignments are
in § 13.

**Effort summary** by phase:

| Item | Phase | Milestone | Δ LOC | Δ time | New infra? |
| - | - | - | - | - | - |
| Scoring + metric snapshots | 1 | M8 | ~300 | 1.5–2 d | no |
| Caption feature extraction | 1 | M4 | ~50 | 0.5 d | no |
| Posting rhythm view | 1 | M8 | ~30 | 0.25 d | no |
| Audio/music tracking | 1 | M4 | ~80 | 0.5 d | no |
| Hashtag trend view | 1 | M8 | ~30 | 0.25 d | no |
| Near-duplicate (simhash) | 1 | M4 | ~50 | 0.5 d | no |
| Webhooks | 1 | M9 | ~150 | 1 d | no |
| Cost / usage tracking | 1 | M3 | ~100 | 0.5 d | no |
| GDPR `expires_at` | 1 | M9 | ~20 | 0.25 d | no |
| Read-replica wiring | 1 | M1 | ~30 | 0.25 d | yes (Postgres replica) |
| Canary account | 1 | M2 + M7 | ~80 | 0.5 d | no |
| Caption embeddings | 2 | M11 | ~250 | 1.5 d | yes (`pgvector`, embedding API) |
| LLM-derived features | 2 | M12 | ~200 | 1.5 d | yes (LLM API key) |
| Author style + re-ranking | 2 | M13 | ~150 | 1 d | no (reuses M12 LLM key) |

**Phase 1 total**: ~12–15 dev-days, ~3000 LOC, no external API dependencies
beyond Instagram itself. Produces a complete, scored, MCP-accessible
scraper.

**Phase 2 total**: ~4–5 dev-days, ~600 LOC, adds embedding API + LLM API
dependencies. Layered cleanly on top of Phase 1; can be scheduled
independently and is fully optional from Phase 1's perspective (the AI
generator can consume Phase 1 data over MCP without it).

**1. Caption feature extraction (cheap, high value)**
On every post upsert, derive and store: `language` (langdetect),
`emoji_count`, `hashtag_count`, `mention_count`, `caption_length`,
`has_question`, `has_cta` (regex on common CTAs in TR + EN). Stored as
columns on `ig_posts` so they're queryable. Feeds both scoring (caption
quality is a weak signal) and the AI generator's prompt construction.

**2. Caption embeddings (medium cost, very high value for the generator)**
A vector embedding of every caption stored in pgvector. Lets the AI
generator do "find me the 20 best-performing posts most similar to this
draft caption" in one SQL query. Adds the `pgvector` extension to
Postgres and an `ig_post_embeddings(post_id, embedding vector(1536),
model)` table. Generated lazily by a separate job type
(`embed_post_batch`) so embedding cost is decoupled from scrape latency.

**3. Posting rhythm per author**
A small materialised view `ig_author_posting_pattern(author_id,
hour_of_day, weekday, post_count, avg_score)` answering "when does
competitor X post, and when do they get the best results". Trivial to
build from existing tables, very useful as context for generation.

**4. Audio / music tracking for reels**
`ig_audio_tracks(id, title, artist, original_audio_user_id, use_count)`
populated from `media.music_info`. Lets you see which audio is currently
trending in your niche — usually the single strongest reel-virality
factor. Cheap: data is already in `ig_posts.raw`, just normalise it out.

**5. Trend detection on hashtags**
`ig_hashtag_velocity` materialised view: change in post-count and
average-score per hashtag week-over-week. Cheap, daily-refreshed,
surfaces rising tags before they peak.

**6. Near-duplicate / repost detection**
Hash captions (simhash) and store on `ig_posts.caption_hash`. Catches
competitors recycling the same caption across accounts and lets the
generator avoid suggesting copy that already exists.

**7. Webhooks / event bus**
A `POST {url}` callback when a post crosses a score threshold (e.g. ≥85)
or when a tracked target's job finishes. Lets `agent-platform` react in
near-real-time without polling. Implemented as a tiny `ig_webhooks` table
+ a fire-and-forget HTTP call from the worker.

**8. Cost & usage tracking**
`ig_usage_daily(date, account_id, calls_made, posts_saved, comments_saved,
proxy_bytes)` aggregated nightly. Becomes essential the moment you ask
"how much is this scraper actually costing per scraped post" and want to
decide whether to add accounts or proxies.

**9. Data retention / GDPR posture**
A configurable TTL on `ig_comments.text` and `ig_users.biography` (the
two free-text fields most likely to contain PII). Default off, easy to
turn on if a takedown request lands. Worth deciding **the data model**
for it now (`expires_at` columns) even if we leave them null.

**10. Read replica / BI access**
Once `ig_posts` is in the millions of rows, BI / dashboard queries will
contend with the scraper writes. Plan to point Metabase / Grafana / ad-hoc
SQL at a Postgres read replica rather than the primary. No code change —
just an env var pointing the read-only API endpoints at the replica DSN.

**11. Canary account**
Reserve one `ig_accounts` row tagged `role='canary'` that runs only
synthetic, low-rate scrapes against a known-stable target every hour. If
the canary fails, instagrapi probably broke against the latest IG private
API — alert before the rest of the fleet starts churning.

## 15. Open questions to resolve before M1

1. **Multi-tenancy**: is there a notion of "client / brand" that owns a set
   of targets, or is data global? If multi-tenant, add `tenant_id` to
   `ig_scan_targets`, `ig_scrape_jobs`, and `ig_posts` now — cheap to add,
   painful to retrofit.
2. **Auto-promote vs review queue**: should hashtag-discovered users land
   in `status='pending_review'` (human approval, default) or
   `status='active'` (immediately scanned)? Driven by
   `IG_AUTO_PROMOTE_DISCOVERED`. Recommendation: start with `false` until
   the follower/media thresholds are calibrated.
3. **Story retention**: stories expire in 24h on Instagram and we keep them
   forever in `ig_stories`. Confirm that's the desired behaviour and that
   we don't need a TTL/archive policy yet.
4. **Reels-only accounts at scale**: scanning both `user_medias` and
   `user_clips` doubles request count per user scan. Acceptable for daily
   incremental (cheap due to early-stop cursor); flag if you'd rather skip
   `user_clips` for accounts whose feed already includes reels.

(Auth was previously listed here; resolved → static API key, see § 8.)
