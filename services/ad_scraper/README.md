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

Valid `media` codes are **1-19, 21-23, 25-26, 28-34** — 20, 24, 27, 35 and 36 are
rejected. An invalid code
does **not** get ignored — it fails the whole request with
`"Parameter error, please clear the filter and refresh"`.

All 31 codes, each name read back from a live payload (never guessed), and
served to the panel by `GET /api/v1/filters`:

| Code | Network |
| - | - |
| 1 | Instagram |
| 2 | Facebook |
| 3 | X |
| 4 | AdMob |
| 5 | Unity Ads |
| 6 | ironSource |
| 7 | AdColony |
| 8 | AppLovin |
| 9 | Vungle |
| 10 | Facebook (FAN) |
| 11 | YouTube |
| 12 | Chartboost |
| 13 | TikTok |
| 14 | Yahoo Japan |
| 15 | Line Japan |
| 16 | Messenger |
| 17 | Pinterest |
| 18 | TopBuzz Japan |
| 19 | SmartNews Japan |
| 21 | AdSense |
| 22 | Mintegral |
| 23 | Pangle |
| 25 | Snapchat |
| 26 | Moloco |
| 28 | Kwai |
| 29 | SnackVideo |
| 30 | Yandex |
| 31 | VKontakte |
| 32 | Threads |
| 33 | Bigo Ads |
| 34 | InMobi |

`GET /filters` merges these with what we have actually ingested, so each
option also carries a usage count and a `source` of `observed` or `seed`.

### Two things about `media` that look like bugs

**A row reports its creative's whole network set, not the part you asked
for.** Filtering `media: [13]` returns 50 TikTok rows — and those same rows
also list Pangle (17), Instagram (6), Facebook (6): one creative runs on
several networks, and the payload gives all of them. If the UI filters by
TikTok and then renders the row's `media` array, users will read
"TikTok, Pangle, Instagram" and conclude the filter is broken. It is not.
The match is "any of its networks is in your filter".

**A small network vanishes inside a large filter.** `media:
[4,11,21,2,1,10,16,32]` (Google + Meta) reports 194 178 669 rows; adding 13
takes it to 198 422 085, so TikTok is 2.1% of the union — and page 1 under
`order: max_dt_desc` came back with **zero** TikTok rows. Nothing is wrong;
the newest 50 of 198M simply belong to the big networks. To see a specific
network, filter to it alone.

## `purpose` — the product's top tabs

Read out of the web app's own Vue source, so this is the definition rather
than an inference:

```
purposeEnum = {"Game": 1, "App": 2, "Website": 3, "Ec": 4, "Account": 5, "Domestic": 6}
```

| Value | Tab | Available to us |
| - | - | - |
| 1 | Game | yes |
| 2 | App | yes — `AD_DEFAULT_PURPOSE` |
| 3 | Website | yes |
| 4 | Ec (e-commerce) | no — hidden in the UI, refused with `00:401001` |
| 5 | Account | no — hidden in the UI, refused with `00:401001` |
| 6 | Mainland China games | accepted, but answers `total: 0` here |

This corrects an earlier reading in these docs. Probing corpus sizes had
suggested "a gradient from in-app inventory toward web/display", which
described the *effect* and missed the cause: they are advertiser verticals.
The measurements still hold and now make sense — game ad networks peak under
Game (AdColony 8 732 rows versus 48 under Website; Unity Ads 2 747 539) and
display peaks under Website (AdSense 28 342, X 304 137) because those are the
networks each vertical buys.

Two consequences worth knowing: under Website the UI narrows its media list
to 18 (game networks are hidden), and `format` 302 Rewarded / 401 Playable
only exist under purpose 1-4.

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

## "It fetched one page even though I asked for 30"

Three things stop a walk early, and two of them are correct:

1. **`total` is smaller than one page.** `stats.total_reported` is the
   upstream's own row count for your filter. If it is 26, one page of 50
   holds everything and stopping is right — past the end the API *repeats
   the last page* rather than returning empty, so continuing would fetch the
   same 26 rows another 29 times and report `materials_seen: 1500`. Check
   `total_reported` first; it answers this question by itself.
2. **An empty page.** Recorded as `ad_page_empty` in the log.
3. **Every row on a page was one this same job already stored**
   (`ad_page_all_repeats`) — a backstop for a `total` that drifts on a live
   feed.

Deep pagination itself works. Measured: `page_to: 30` on a filter reporting
733 868 rows walked all 30 pages, 1 500 materials, 110 369 facet edges,
6.62s of that spent held at the rate gate. If you see one page against a
large `total_reported`, that is a bug — quote the job id.

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

## Authentication — a stored session token

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

**No caching, deliberately.** The token is read from the row on every
request. There used to be an in-process cache, and it had a real bug: a
module-level cache is per **process**, and this service runs two. Pasting a
fresh token hits the API process, which primed *its* copy — while the worker,
the process that actually makes upstream requests, kept serving the old one.
A rotated token therefore did not reach the worker at all until a job failed
and the rejection path happened to invalidate it. There was even a
`POST /credentials/session/invalidate-cache` endpoint whose docstring named
"another replica storing a newer token" as its use case, which is exactly the
case it could not fix. It has been removed along with the cache.

What the cache bought, measured: **0.66 ms** per read. The rate gate already
holds requests **1500 ms** apart, so it saved 0.04% of one request interval
in exchange for cross-process staleness. Rotating a token now takes effect on
the next request in every process.

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

## `GET /filters` — the vocabulary, served

The filter vocabulary used to live in two places: a hand-written option list
in the panel and the measured truth here. They drifted, and the drift was not
symmetric — the panel was missing nine networks (ironSource, Vungle,
Chartboost, AdSense, Mintegral, Pangle, Moloco, Bigo, InMobi) while asserting
four it had guessed at. Three of those guesses later measured correct, which
is the worst case: a list that is right often enough to be trusted.

`GET /api/v1/filters` returns every filter a job can carry with its options
and constraints, assembled from two sources: the facet values we have
actually ingested (with usage counts) merged over a small measured seed in
`app/services/filter_schema.py`, so a fresh database still yields a usable
form. Observed values win; a seed only fills a gap. Nothing in the seed is
guessed — every name came back from a live payload.

`media` also carries `valid_codes`: the complete accepted set including codes
whose names we have never seen. A panel must not offer anything outside it,
because one invalid code fails the whole upstream request.

The vocabularies come from two places now. Facet values still merge observed
data over a seed, but the enumerations the upstream never returns in a
payload — `format`, `creativeType`, `order`, `language`, `category`,
`campaignType`, `appCashWay`, `videoTime`, `materialRatio`, `asrLanguage`,
the boolean flags and the `searchDsl` keys — were read out of the web app's
own source and live in `app/services/filter_schema.py`.

That file also records two things a caller cannot discover by trying:

* **`url_only_params`** — keys that appear in the web UI's URL but are NOT
  variables of the GraphQL document. `daterange` is the trap: it looks like a
  filter, and sending it does nothing at all. It is UI sugar that compiles to
  `startDate`/`endDate`.
* **`material_types`** — the full map for `material.type`. Our docs used to
  say "102 image, 202 video", which silently mislabels 201 Video, 203
  Fullscreen Video, 103 Animated Image, 104 Multiple Image, 105 Carousel and
  301 Html.

`media` options now carry a `category` (Social Media / Ad Networks / Local
Media) and the endpoint serves the UI's one-click groups as `presets`:
Meta Ads `[2,1,10,16,32]`, Google Ads `[4,11,21]`, TikTok for Business
`[13,23,18]`.

Our GraphQL document is at parity with the web app's — 59 variables, none
missing on either side.

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

## `media_format` is the file, `media[]` is the network

Easy to trip over, and it made the list response look like it was missing
data it never had. `media_format` / `media_width` / `media_duration_sec`
describe the **file** — `mp4`, 540, 23s. The **network** (TikTok, Facebook,
Unity Ads) is the `media` facet.

`GET /materials` now attaches the small facets to every row — `media`,
`platform`, `channel`, `format` as `[{code, name}]` — plus `advertisers`
(capped at 5) and `advertiser_count`. Two extra queries per page, not per
row.

`area` is excluded on purpose: measured over 1 923 materials it averages
56.1 edges and peaks at 136, so a 50-row page would carry ~2 800 country
entries. It stays on `GET /materials/{id}`, which returns every facet
including `area` and `resource_element`.

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

## Where the ad copy lives

`slogan` is the ad text and the only text the source gives — populated on
every row observed. `asr` is the platform's auto-transcript, present on
about one video creative in ten (13 of 142 videos observed; 14 of 178 rows
overall, and never on an image).

**There is no title field.** Probing the live schema shows
`creative.title/text/copy/adText` and `material.title/description/marketingWord`
are all rejected by validation; the web UI's own query asks for the same
`slogan` + `description` pair we do. `description` and `txt_url` are declared
by the upstream but came back empty on every row observed, so don't build a
layout that needs them.

## Is there a link back to the original ad?

Short answer: **no — the upstream does not provide one.** Worth stating
because it is a reasonable thing to expect and the schema half-promises it.

| What you might want | Do we have it? |
| - | - |
| The ad's click-through / landing page | **No.** `creative.txtUrl` exists in the schema and is **empty on every row** — verified in our own data (0 of 178) and live against the API. No `link`, `url`, `landingUrl`, `clickUrl`, `jumpUrl`, `deeplink`, `targetUrl` or `adUrl` field exists at all; all are rejected by GraphQL validation |
| A permalink to the creative on AppGrowing | **No.** The `materialIds` filter returns 0 rows even for an id we hold, under every `purpose`, so the id is not addressable that way and a deep link can't be constructed from it |
| The creative file itself | **Yes** — `media_url` / `poster_url` (signed, dies at `media_url_expires_at`) and, better, our permanent S3 copy via `media-url` |
| Where the ad points, indirectly | **Partly** — the advertiser's store pages, `gp_app_url` / `ios_app_url` on `ad_advertisers`. Sparse: each populated on 7 of the 48 advertisers observed |

So the outward-facing identity of an ad here is its **advertiser**, not a URL.
A creative detail view can offer "open on Google Play / App Store" when the
advertiser has those, and otherwise has nothing to link to.

## Mirror downloads run concurrently — and why that is worth only ~1.4x

Downloading is the slow half of a mirroring job by a wide margin: 4.46s per
creative measured against the live CDN, so a full page of 50 is close to four
minutes of pure waiting. Those downloads now go out `AD_MIRROR_CONCURRENCY`
at a time (default 4) instead of one after another.

**Do not expect Nx.** The constraint is bandwidth, not latency. Measured with
distinct files at each level, so no CDN caching bias:

| Concurrent | Throughput |
| - | - |
| 1 | 2.63 MB/s |
| 4 | **3.80 MB/s** |
| 8 | 2.80 MB/s |

So roughly 1.2-1.6x, peaking around 4, and pushing higher stops helping. A
real job confirms the ceiling rather than beating it: 8 creatives, 11.3 MB,
3.74s — 3.03 MB/s, the same band. That job finished quickly because the files
were small (1.42 MB average, short videos), not because concurrency
multiplied anything.

The shape matters as much as the number. `_persist_page` now runs in two
phases: every upsert first, one transaction each, in order — they are cheap
(0.03-0.07s per material) and a SQLAlchemy Session must not cross threads —
then the downloads, bounded, with `persist_keys` back on the event loop.
`mirror.transfer` touches no database precisely so this split is possible.

`stats.mirror_seconds` records the download time per job, separately from
`throttle_wait_seconds`. Mirroring dominates a job's wall clock, so "the job
was slow" and "the downloads were slow" need to be different answers.

Failures stay per-creative: a raising transfer costs one `mirror_failed`
counter, not the page.

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

## Staying under the rate limit

The upstream answers `00:400998` — "High visiting frequency, please try
again later" — when we ask too fast. Retrying after that is table stakes;
the point of `app/services/youcloud/throttle.py` is to hit it rarely.

**The gate is process-wide, not per job.** That is the whole fix. Pacing
used to be a sleep between pages inside `paginate_materials`, so each job
spaced its *own* requests — and with `AD_WORKER_CONCURRENCY=2` two jobs in
one process pushed twice as hard as configured, silently, with concurrency
as a hidden rate multiplier. Now one shared gate holds its lock across the
wait, so the process rate is `1 / interval` no matter how many jobs run;
concurrency changes how many jobs make progress, not how hard we push.

On a refusal the gate does two things, and the second is the one that
helps: the interval widens geometrically toward a ceiling, **and the shared
gate is pushed out by a cooldown so sibling jobs pause too.** Backing off
only the refused request would leave its sibling hammering the endpoint
that just asked us to slow down. After a run of clean responses the
interval walks back down, so one bad minute does not throttle the service
until the container restarts.

Rate limits get their own, larger retry budget
(`AD_API_RATE_LIMIT_MAX_RETRIES`, default 5) separate from
`AD_API_MAX_RETRIES`. The reason is arithmetic, not optimism: a rate limit
that exhausts the transport budget fails the job, and the requeued job
restarts at `page_from` — spending *more* requests against the endpoint
that asked for less. Waiting in place is strictly cheaper.

| Knob | Default | What it does |
| - | - | - |
| `AD_API_MIN_REQUEST_INTERVAL_SECONDS` | 1.5 | Floor between any two requests, process-wide |
| `AD_API_MAX_REQUEST_INTERVAL_SECONDS` | 20 | Ceiling the penalty can reach |
| `AD_API_RATE_LIMIT_COOLDOWN_SECONDS` | 30 | How long the whole process pauses on a refusal |
| `AD_API_RATE_LIMIT_MAX_RETRIES` | 5 | The rate limit's own retry budget |
| `AD_API_JITTER_RATIO` | 0.25 | Added, never subtracted — separate containers share no state |

These counters live in the **worker** process, not the API's — the worker
is what fetches pages. Scrape `ad-scraper-worker:9103/metrics`
(`AD_WORKER_METRICS_PORT`); the API's own `/metrics` reports 0 for all of
them forever, because nothing there ever increments them.

Metrics to watch: `ad_throttle_wait_seconds_total` rising while
`ad_rate_limited_total` stays flat means the pacing is working. Both rising
means the floor is too small for this account — raise
`AD_API_MIN_REQUEST_INTERVAL_SECONDS` rather than the retry budget.
`ad_throttle_interval_seconds` sitting at the ceiling means we are being
throttled hard and the job queue is effectively serialised.

Mirror downloads are not paced: they go to the CDN
(`creative-ag-global-esa.umcdn.cn`), a different host, and every `00:400998`
observed came from the GraphQL endpoint. If CDN refusals ever appear, they
need their own gate — this one deliberately guards one endpoint.

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
