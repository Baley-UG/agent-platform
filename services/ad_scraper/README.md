# ad_scraper

Ad-intelligence ingestion for the agent-platform. Pulls competitor ad
creatives from AppGrowing/YouCloud's GraphQL API into the shared Postgres,
mirrors their media into our S3 bucket before the source URLs expire, and
feeds them to `content_pipeline` as generation references.

Two processes from one image:

| Process | Command | Port |
| - | - | - |
| API | `uvicorn app.main:app` (default) | 8083 |
| Worker | `python -m app.worker` | — |

No scheduler, no Redis, no separate database. Ingestion is operator-driven:
you post a job with a filter set, the worker walks it.

## Why the media mirror exists

YouCloud signs its CDN URLs as `?auth_key=<epoch>-…` and stops serving them
roughly **15 days** after issue. A creative you can watch today is
unrecoverable in two weeks. So `AD_MIRROR_MEDIA` defaults to `always`, and
every material row carries `media_url_expires_at` so a UI can tell a live
link from a dead one.

Three things measured against the live CDN, all of which the downloader
depends on:

| Probe | Result |
| - | - |
| Tampered `auth_key` | **401** — the signature is enforced, which is the whole premise |
| Deliberately wrong `referer` | **200** — referer is *not* validated, so we don't spoof it |
| Browser `accept: image/avif,image/webp,…` | **61 KB webp** instead of the **156 KB jpeg** |

Two behaviours worth knowing, both found by actually running a re-ingest:

* **A creative already in our bucket is not re-downloaded.** Re-running a
  filter is the normal way to pick up newly-published creatives; without the
  skip, every re-run re-fetched every video it already held. The job reports
  those as `stats.mirror_cached`.
* **`mirror` on a job is tri-state.** Omit it to follow `AD_MIRROR_MEDIA`;
  `false` opts this job out even under the `always` policy; `true` opts in
  under `job`. `never` ignores it — turning storage off is an operator
  decision a job shouldn't override.

That last one is a trap: the CDN content-negotiates on `Accept`, but
`ad_materials.media_format` comes from the API payload (`"jpeg"`). Accepting
a webp substitution would leave the column describing bytes we don't have,
so the downloader pins `Accept` to the original representation. The stored
`content_type` is logged on every upload, so a CDN-side change shows up in
Loki instead of quietly diverging.

## Which facet is "the platform"? (read this one)

| Facet | What it actually means | Example values |
| - | - | - |
| **`media`** | **the social network / ad network** — this is the one you want for "show me TikTok ads" | Instagram(1) · Facebook(2) · X(3) · TikTok(13) · Messenger(16) · Pinterest(17) · Snapchat(25) · YouTube(11) · Threads(32) · AdMob(4) · AppLovin(8) · Unity Ads(5) · Kwai(28) · Yandex(30) · VKontakte(31) |
| `platform` | the **operating system** | iOS(2) · Android(1) |
| `channel` | the ad-buying platform | Meta Ads(1101) · Google Ads(1103) |
| `format` | the ad slot | In-Feed(106) · Native(105) · Banner(102) · Interstitial(103) |
| `area` | country | `TR` · `US` · `DE` … |
| `resource_element` | creative element tags | Phone · Person … |

**`platform` does NOT mean the social network.** It is the OS. Filtering by
TikTok/Facebook/Instagram is the **`media`** facet. This naming comes from
the upstream API and is easy to get backwards — a `platform=1` filter reads
like "Android", not like "Facebook".

Corpus sizes measured against the live API, so you can see what a filter is
worth: Instagram 215M, Facebook 212M, Messenger 163M, Facebook FAN 161M,
AdMob 51M, YouTube 21M, **TikTok 6.0M**, Pinterest 1.7M, Yandex 1.0M,
VKontakte 0.67M, Snapchat 9k, X 0.3M.

Valid `media` codes are 1-19, 21-23, 25-26, 28-31, 33-34. An invalid code
does **not** get ignored — it fails the whole request with
`"Parameter error, please clear the filter and refresh"`.

`purpose` selects the corpus and 1, 2, 3 are the only valid values (4+ is a
parameter error). The Meta family (Facebook / Instagram / Messenger /
Threads) shows up under `purpose: 3`; `purpose: 2` carries the
AdMob/YouTube/Unity side. TikTok answers under both.

## The pagination ceiling — read this before writing a filter

Verified against the live API:

* `page` is capped at **200**; page 201 returns *"Parameter error, please
  clear the filter and refresh"*.
* `limit` is fixed server-side at **50**.

That is a hard **10 000-row ceiling per filter set**. The unfiltered corpus
is ~135 million rows, so a broad filter cannot be ingested by asking for
more pages — it has to be **partitioned**: by `startDate`/`endDate` window,
`area`, `media`, `platform`, or `keyword`, one job each.

The service will not let this fail quietly:

* `POST /jobs` rejects `page_to > 200` with a 422 that says what to do.
* When a job's filter reports more rows than its window covers, the job
  still succeeds but records `stats.truncated = true` with a note, logs
  `ad_filter_too_broad`, and increments `ad_filter_truncated_total`.

**Past the end, the API repeats the last page instead of returning empty.**
Measured: a filter with `total: 26` answers pages 1, 2 and 3 with the
identical 26 rows. So `page_to` is an upper bound, not an instruction — the
walk stops as soon as `page × limit >= total`. Without that, `page_to=200`
on a 26-row filter would spend 200 requests fetching the same 26 creatives
200 times, and report `materials_seen: 5200`. Rows re-served within one job
are counted separately as `stats.materials_repeated` and are not re-upserted.

Also worth knowing: the feed is live and `order=max_dt_desc` re-sorts
between requests, so rows shift across pages of a large result set.
Materials are deduped by id, so this is harmless — but page count is not a
row count. A date-bounded filter is more reproducible.

## Setup

1. Set `AD_SCRAPER_API_KEY` and `AD_SECRET_KEY` in the repo `.env` (see
   `.env.example` for the full `AD_*` block). Generate the Fernet key with:

   ```bash
   python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
   ```

2. Apply the migration:

   ```bash
   docker compose run --rm ad-scraper-api alembic upgrade head
   ```

   Note this service uses its own `ad_alembic_version` table. It shares the
   `public` schema with `ig_scraper`, which owns the default
   `alembic_version` — one table for both would make each service read the
   other's revision id as unknown.

3. Give it a session (see below), then start it:

   ```bash
   docker compose up -d ad-scraper-api ad-scraper-worker
   ```

## Authentication — a cached session token

One cookie value, `sessionId`, is the entire auth surface. It is a JWT; its
`exp` claim is read (without signature verification — we only want to know
when the server stops accepting it) and stored alongside the token, so the
service can warn before it dies instead of discovering it mid-job.

**There is no login flow and no stored password.** Automatic login was
considered and dropped: it meant replaying a password against a flow we
cannot inspect (introspection is disabled), on a platform whose ToS it may
breach, with account lockout as the failure mode — to save a weekly paste.
Nothing here stores a password, so there is none to leak.

Rotate the token:

```bash
curl -X PUT http://localhost:8083/api/v1/credentials/session \
  -H "X-API-Key: $AD_SCRAPER_API_KEY" \
  -H 'content-type: application/json' \
  -d '{"session_cookie":"<sessionId cookie value from a logged-in browser>"}'
```

```bash
curl -s http://localhost:8083/api/v1/credentials -H "X-API-Key: $AD_SCRAPER_API_KEY"
```

```json
{
  "status": "active",
  "has_session": true,
  "session_expires_at": "2026-08-25T07:28:35Z",
  "expires_in_seconds": 604800,
  "needs_refresh": false,
  "consecutive_failures": 0
}
```

The token itself is never returned. `expires_in_seconds` goes negative once
it is dead rather than being clamped, so a panel can say "expired 3 days
ago". `/ready` reports `youcloud_session` as
`active | expiring | missing | locked_out` — a dead token does not fail
readiness, because already-ingested data still serves fine; only new jobs
are blocked.

**Caching.** The decrypted token is held in process memory with its expiry,
so the hot path costs neither a DB round-trip nor a Fernet decrypt. It is
invalidated when a new token is stored, when the API rejects the current
one, and when its own expiry passes. `POST /credentials/session/invalidate-cache`
exists for the out-of-band case (a direct SQL edit, or another replica
storing a newer token).

**When the token dies**, a job fails terminally rather than retrying — only
an operator can mint a new one, so retrying would burn the attempt budget to
reach the same conclusion. The failure also marks the credential row, and
after `AD_LOGIN_MAX_CONSECUTIVE_FAILURES` rejections it flips to
`login_failed`, which stops further jobs from replaying a token the server
has already refused.

## Filtering by app id — use `app_id`, never `campaign`

This one is a trap worth stating loudly. The web UI filters by app through
its **advanced** panel, which is the GraphQL `searchDsl` variable:

```
advanced=[{"key":"appid","value":"1661308505","type":"equal"}]
```

Handing that same numeric id to `campaign` instead is **accepted and returns
zero rows** — no error, no warning, an empty result that reads exactly like
"this app has never advertised". Measured against the live endpoint:

| Variable | appid 1661308505 |
| - | - |
| `searchDsl: [{"key":"appid","value":"1661308505","type":"equal"}]` | **6 559 creatives** |
| `campaign: "1661308505"` | **0 creatives, HTTP 200, no error** |

`campaign` takes the platform's own opaque entity id
(`M3OgFwcr4yLVdauQFomkHA==`), not a store id. So:

* Set the job's **`app_id`** field and the correct `searchDsl` entry is built
  for you (appended, so a hand-pasted `searchDsl` survives).
* A numeric value in `filters.campaign` is **rejected with a 422** that says
  to use `app_id` — because a silent zero is worse than a loud failure.

```bash
curl -X POST http://localhost:8083/api/v1/jobs \
  -H "X-API-Key: $AD_SCRAPER_API_KEY" -H 'content-type: application/json' \
  -d '{"app_id":"1661308505","filters":{"purpose":2,"media":[1]},"page_to":3,"order":"impression_inc_2y_desc"}'
```

Going the other way — you have a numeric store id and want the opaque one —
`ad_advertisers` keeps `ios_app_url` / `gp_app_url`, so the id is already in
the row: appid `1661308505` resolves to *ChatOn AI - Chat Bot Assistant*,
whose `ios_app_url` ends `/id1661308505`.

## Running a job

`filters` is the `materialList` GraphQL `variables` object, verbatim — build
a query in the AppGrowing UI, copy the variables from the network tab, paste
them here. Omit `page` and `order`; those come from the job's own fields.

```bash
curl -X POST http://localhost:8083/api/v1/jobs \
  -H "X-API-Key: $AD_SCRAPER_API_KEY" \
  -H 'content-type: application/json' \
  -d '{
        "filters": {
          "purpose": 2,
          "media": [2],
          "area": ["TR"],
          "startDate": "2026-07-01",
          "endDate": "2026-08-18",
          "accurateSearch": 1,
          "field": "all"
        },
        "page_from": 1,
        "page_to": 20,
        "order": "max_dt_desc"
      }'
```

Then watch it:

```bash
curl -s http://localhost:8083/api/v1/jobs/<job_id> -H "X-API-Key: $AD_SCRAPER_API_KEY" | jq .stats
```

```json
{
  "pages_fetched": 20,
  "materials_seen": 1000,
  "materials_new": 947,
  "materials_updated": 53,
  "mirrored": 991,
  "mirror_failed": 9,
  "total_reported": 4210,
  "truncated": true,
  "notes": ["filter set reports 4210 rows; this job's window covers 1000. Raise page_to (max 200) to reach the rest."]
}
```

## Filter values the upstream rejects outright

`filters` is forwarded verbatim and only `purpose` is required. But the
upstream validates several fields strictly — **one bad value fails the whole
request** with a bare *"Parameter error, please clear the filter and
refresh"*, so `POST /jobs` checks them first and returns a 422 that says
what's wrong:

* **`area`** — uppercase ISO-2, **one code per array element**. `["TR;SA"]`,
  `["tr"]`, `["TUR"]` are all rejected upstream. A panel that string-joins a
  country multi-select produces the first one; send `["TR", "SA"]`.
* **`media` / `platform` / `format` / `creativeType` / `resourceElement` /
  `category`** — arrays of positive integers, not strings.
* **`isAllDate` + `startDate`/`endDate` together** — `isAllDate` silently
  overrides the range upstream, so the combination is refused here rather
  than letting a date filter vanish.
* **`startDate` after `endDate`** — accepted upstream, returns nonsense.

Silent-zero cases no validation can catch: an unknown `gender` code returns
`total: 0`, and a numeric `campaign` does too (use `app_id`). `field` and
`accurateSearch` are optional despite showing up in every UI-copied example.

With neither `isAllDate` nor a date range you get the upstream's default
recent window — 18M rows on `purpose: 2` versus 205M with `isAllDate: 1`. So
omitting both is a filter, not "everything".

## Reading the data

```
GET /api/v1/materials?type=202&area=TR&min_impressions=500000&sort=impressions_desc
GET /api/v1/materials/{id}
GET /api/v1/materials/{id}/media-url          # presigned GET for the S3 mirror
GET /api/v1/advertisers?kind=AppBrand&search=drama
GET /api/v1/advertisers/{id}/materials
GET /api/v1/dimensions?kind=area              # filter dropdowns, with usage counts
```

Facet filters combine as **AND across kinds, OR within a kind**:
`?media=2&area=TR&area=DE` means "on media 2, in Turkey or Germany".

Through the platform gateway (global admin only), every route above is also
at `/api/v1/ad-scraper/...` on port 8000, and appears in the main app's
`/docs`.

## Field names that differ from the API's

The wire format has two traps. Both are renamed on the way in:

| API field | Column | What it actually is |
| - | - | - |
| `material.duration` | `run_days` | **Days on air** (`end_date - start_date`), not a video length |
| `creative.resource[].duration` | `media_duration_sec` | Video length in **seconds** |
| `impression_inc_2y` | `impression_inc_2y_raw` + `impression_inc_2y` | A display string (`"1.1M"`); the parsed `bigint` is what sorts correctly |
| `campaign[]` | `ad_advertisers` (M2M) | Every app / brand / site / drama running the creative — up to 66 per creative |
| `asr` | `asr` | The platform's auto-transcript. Populated on roughly a fifth of video creatives, and the most useful field downstream |

The untouched payload is kept in `ad_materials.raw`, so improving a mapping
is a JSONB backfill rather than a re-scrape.

## Feeding content_pipeline

```bash
curl -X POST http://localhost:8082/api/v1/projects/<pid>/references/import-from-ads \
  -H "X-API-Key: $CP_API_KEY" \
  -H 'content-type: application/json' \
  -d '{"material_id":"a908ba4485611aeefed5e2df204550d2","auto_approve":false}'
```

This needs no download — the creative is already mirrored, so the import is
a server-side S3 copy into the project's prefix plus a row with
`source_provider='appgrowing'`. The ad's `asr` becomes the reference's
`transcript`, and impressions / days-on-air / advertisers / areas land in
`metadata` where auto-generation ranking can read them.

## Error handling worth knowing

**The endpoint answers HTTP 200 for every failure.** An expired session, an
insufficient plan and a rejected filter all come back `200` with an `errors`
array, so `raise_for_status()` would wave a dead credential through as a
successful empty result. Classification reads the body — see
`app/services/youcloud/errors.py`:

| `extensions.c` | Meaning | Job outcome |
| - | - | - |
| `05:*` (e.g. `05:400001`, `05:403001`) | Token missing / malformed / no longer accepted | Terminal fail + credential marked rejected |
| `00:403001` | Plan does not cover the query (also what an unauthenticated request gets) | Terminal fail |
| *(no code)* `"Parameter error…"` | Filter or page rejected | Terminal fail |
| *(no code)* `"The system is busy…"` | Server-side hiccup | Retry with backoff |

One non-JSON case: dropping the `accept-language` header yields HTTP 406
with a plain-text body. The client always sends it.

## Building a UI against this

The admin panel talks to the **gateway on port 8000**, not to this port, and
authenticates with an admin JWT — it never holds `AD_SCRAPER_API_KEY`. The
full contract (endpoints, query params, presign flow, job payloads, the
field-name traps, 12 pitfalls) is in
[`ADMIN_API_GUIDE.md`](./ADMIN_API_GUIDE.md).

## Development

```bash
cd services/ad_scraper && uv venv .venv && uv pip install -e '.[dev]'
```

```bash
.venv/bin/python -m pytest tests/ -q
```

```bash
.venv/bin/python -m alembic upgrade head --sql
```

The test suite runs without a database or network: `tests/fixtures/material_list_page.json`
is a real captured API page (trimmed, with the account-linked `auth_key`
signatures redacted) and the client's HTTP layer is stubbed.
