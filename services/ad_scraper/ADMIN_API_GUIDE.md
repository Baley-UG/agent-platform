# ad_scraper — Admin Panel API Guide

Contract for the admin panel (`agent-platform-admin`). Everything the panel
needs to browse ad creatives and start ingestion jobs.

Sibling of `services/content_pipeline/ADMIN_API_GUIDE.md`. When this service
changes in a contract-affecting way, update this file and sync it into the
panel repo's `docs/` folder.

---

## 1. Architecture — one base URL, Bearer JWT

The panel never talks to ad_scraper directly and **never holds
`AD_SCRAPER_API_KEY`**. Same shape as `/cp/*` and `/instagram-scraper/*`:

```
┌──────────────┐   Bearer   ┌──────────────────────────────┐
│ Admin Panel  │ ─────────▶ │  Main app (port 8000)        │
│              │    JWT     │  • validates admin JWT       │
└──────────────┘            │  • forwards with X-API-Key   │
                            └──────────────┬───────────────┘
                                           │ internal only
                                  ┌────────▼─────────┐
                                  │ ad-scraper :8083 │
                                  └──────────────────┘
```

* Base URL: `http://localhost:8000` (dev) — same as everything else.
* Prefix: **`/api/v1/ad-scraper/`**. Note the hyphen; it is not `/ad_scraper/`
  or `/ads/`.
* Auth: `Authorization: Bearer <access_token>` from
  `POST /api/v1/admin/auth/login`.
* **Global admin role required.** The gateway returns 403 for a non-admin
  principal — ad_scraper has no per-project scoping, so there is nothing to
  scope a member to. Hide the whole section for non-admins.

```bash
curl http://localhost:8000/api/v1/ad-scraper/materials \
  -H "Authorization: Bearer <access_token>"
```

Types come from the federated schema, so ad_scraper's models are already in
the generated file:

```bash
npx openapi-typescript http://localhost:8000/api/v1/openapi.json -o src/api/types.ts
```

17 `/ad-scraper/*` paths appear there. Auth failure shapes:

| Situation | Status | Body |
| - | - | - |
| No token | 401 | `{"detail":"missing Bearer token or session cookie"}` |
| Non-admin | 403 | `{"detail":"admin role required"}` |
| Service key unset on the backend | 503 | `{"detail":"ad_scraper service token not configured (AD_SCRAPER_API_KEY)"}` |

---

## 2. What this service holds

One row per **material** — an ad creative (an mp4 or a jpeg) as AppGrowing
sees it. Around each material:

| Thing | Meaning |
| - | - |
| **resources** | The creative's file(s). Usually one; a carousel has several |
| **advertisers** | Every app / brand / site / drama running this creative. One creative can carry **66** of them — ad networks resell the same trailer |
| **dimensions** (facets) | six vocabularies flattened into one `(kind, code)` table — see the table below, and note that `platform` is the OS, not the social network |
| **jobs** | One ingestion run: a filter set + a page window |

The media is **mirrored into our own S3** at ingestion time, because
YouCloud's CDN URLs are signed and stop resolving ~15 days out. That is why
the panel should always prefer `media-url` (§ 5) over `media_url`.

### Which facet is "the platform"?

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

For a UI this matters: the "platform" chip a user expects (TikTok, Facebook,
Instagram) is the **`media`** facet. Label it "Network" or "Platform" in the
UI if you like, but wire it to `media`. Reserve a separate "OS" filter for
`platform` if you surface it at all.

---

## 3. Field names that will trip you up

Read this before designing a card or a table. These are wire-format quirks
the backend renames, and getting them backwards produces plausible-looking
nonsense.

| Field | It is NOT | It IS |
| - | - | - |
| `run_days` | video length | **days the ad has been on air** (`end_date - start_date`). A high value means a proven, long-running creative — usually the most interesting sort |
| `media_duration_sec` | days | **video length in seconds**. `0` for image creatives |
| `impression_inc_2y` | exact | parsed integer, for sorting and thresholds only |
| `impression_inc_2y_raw` | — | the platform's display string. **Render this**, not the integer: the platform caps at `">10M"`, and showing `10,000,000` claims precision the source never gave |
| `type` | — | `102` = image/banner, `202` = video |
| `media_url` | reliable | the source CDN URL. Dies at `media_url_expires_at`. Use it only as a fallback |
| `violation` | object | a plain moderation label string, e.g. `"Human Exploitation"`. Null on most rows |
| `asr` | — | the platform's auto-transcript. Populated on roughly a fifth of video creatives |
| `slogan` | a headline beside a body | **the ad copy — the only text the source gives.** Populated on every row observed |
| `description` | populated | declared by the upstream but **null on every row observed** |
| `txt_url` | a landing page | declared, empty on every row observed |

So a creative card reading "209 days on air · 26s · >10M impressions" comes
from `run_days`, `media_duration_sec`, `impression_inc_2y_raw` — three
different fields, none of them named what you would guess.

### There is no ad title

Don't design a card around headline + body. Probing the live schema (an
invalid field fails GraphQL validation, a valid one passes it and then hits
auth — the two error messages differ, which makes the probe reliable) shows
that `creative.title`, `creative.text`, `creative.copy`, `creative.adText`,
`material.title`, `material.description` and `material.marketingWord` **do
not exist**. The web UI's own query asks for the same `slogan` + `description`
pair we do, so it has nothing more either.

Practically: render **`slogan`** as the creative's text, `asr` as an
expandable transcript when present, and treat `description` / `txt_url` as
fields that exist in the schema but will be empty.

---

### Is there a link back to the original ad?

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

---

## 4. Browsing creatives

### `GET /ad-scraper/materials`

| Param | Type | Notes |
| - | - | - |
| `type` | int | `202` video, `102` image |
| `media` | string[] | facet codes, repeatable |
| `area` | string[] | country codes, e.g. `TR` |
| `platform`, `channel`, `format` | string[] | facet codes |
| `advertiser_id` | string | the opaque advertiser id (§ 6) |
| `min_impressions` | int | against the parsed integer |
| `min_run_days` | int | "has been running at least N days" |
| `has_asr` | bool | only creatives with (or without) a transcript |
| `mirrored_only` | bool | only creatives whose media is in our S3 |
| `active_since` | date | still live on/after this date |
| `sort` | enum | `impressions_desc` (default) · `end_date_desc` · `run_days_desc` · `first_seen_desc` · `duration_desc` |
| `limit` / `offset` | int | 1–200 / ≥0 |

**Facets combine as AND across kinds, OR within a kind.** `?media=2&area=TR&area=DE`
means "on media 2, in Turkey **or** Germany". A filter row in the UI should
therefore be multi-select per facet, and the facets AND together.

An unknown `sort` is a 400, not a silent fallback.

**Every row carries its networks.** Each item includes the facets a table
needs, so you never have to fetch the detail endpoint to label a row:

```json
{
  "id": "06430b497bf06356cc7bcc81414cbbc0",
  "type": 202,
  "impression_inc_2y_raw": ">10M",
  "media":    [{"code": "13", "name": "TikTok"}],
  "platform": [{"code": "1", "name": "Android"}, {"code": "2", "name": "iOS"}],
  "channel":  [{"code": "1407", "name": "TikTok Ads"}],
  "format":   [{"code": "106", "name": "In-Feed"}],
  "advertisers": [{"id": "uctQ...", "name": "hiyahealth.com", "kind": "Website"}],
  "advertiser_count": 1
}
```

Two traps worth naming:

* **`media_format` is not the network.** It is the file container (`mp4`,
  `jpeg`). The network lives in `media[]` — that is the TikTok/Facebook
  column. Same for `media_width` / `media_duration_sec`: all of those
  describe the file, not the placement.
* **`area` is NOT in the list response.** Measured over 1 923 materials it
  averages 56.1 country edges and peaks at 136, so a 50-row page would carry
  ~2 800 entries for a column no table can show. Countries are on
  `GET /materials/{id}`. You can still *filter* by `area` in the list.

`advertisers` is capped at 5 per row with `advertiser_count` giving the true
total — one creative in our data carries 60 — so render "3X Skin Tool Emotes
+ 55 more" rather than assuming the array is complete.

Returns a flat array (no envelope, no total). Paginate with `limit`/`offset`;
there is no count endpoint — ask for `limit+1` if you need a "next page"
affordance.

### `GET /ad-scraper/materials/{material_id}`

The full record: everything from the list shape plus `asr`, `violation`, and
three nested arrays — `resources[]`, `dimensions[]` (with facet `name` and
`icon` resolved), `advertisers[]`. 404 when unknown.

### `GET /ad-scraper/filters` — build the form from this

One request returns every filter a job can carry, with its options and its
constraints. **Do not hand-maintain an option list in the panel.** That is
what this replaces: the two copies drifted, and the panel's copy was missing
nine networks while asserting four it had guessed.

```json
{
  "job": {
    "page_to": {"type": "int", "min": 1, "max": 200, "default": 5, "note": "..."},
    "order": {"type": "enum", "options": ["max_dt_desc", "..."], "default": "max_dt_desc"},
    "mirror": {"type": "bool|null", "default": null, "note": "..."},
    "app_id": {"type": "string", "note": "..."},
    "rows_per_page": 50,
    "max_rows_per_filter_set": 10000
  },
  "filters": [
    {"key": "purpose", "type": "int", "required": true,
     "options": [{"code": 1, "name": "App advertisers", "note": "..."}], "notes": ["..."]},
    {"key": "media", "type": "int[]", "label": "Network",
     "options": [{"code": "13", "name": "TikTok", "icon": "...", "materials": 321, "source": "observed"}],
     "valid_codes": [1, 2, 3, "..."], "notes": ["..."]}
  ],
  "rejected_keys": {"keys": ["page", "order"], "note": "..."},
  "read": {"sort": {"options": ["..."], "default": "impressions_desc"}}
}
```

How to use each part:

* **`options`** — render these. `materials` is how many creatives we hold for
  that value, so sort by it to put the useful ones on top; `source` is
  `observed` (from ingested data) or `seed` (measured but not yet ingested —
  still valid upstream, just no local rows yet).
* **`valid_codes`** on `media` — the *complete* accepted set, including codes
  whose names we have never seen. Never offer a code outside it: one invalid
  code fails the WHOLE upstream request with a parameter error, so the job
  dies rather than ignoring it. Offer the unnamed ones as a raw-code input if
  you want full coverage.
* **`notes`** — written for the person building the form. They carry the
  traps (`area` must not be string-joined, `isAllDate` overrides the date
  range, a small network vanishes in a big filter).
* **`rejected_keys`** — sending these inside `filters` is a 422.
* **`pattern`** on `area` — validate client-side with it rather than
  reimplementing the rule.

The endpoint is a plain read with no auth beyond the usual gateway JWT, so
fetch it once on mount and cache it for the session.

**What the endpoint now carries beyond facet values.** The enumerations the
upstream never returns in a payload were read out of the web app's own source,
so you get them without probing:

| Key | Count | Note |
| - | - | - |
| `format` | 8 | Banner, Interstitial, Native, In-Feed, Floating, In-Stream Video, Rewarded, Playable |
| `creativeType` | 8 | 201/202/203 video, 102/103/104 image, 301 Html, 105 Carousel |
| `language` | 32 | ad language |
| `asrLanguage` | 25 | voiceover language — a shorter list than `language` |
| `category` | 21 | store category |
| `campaignType` | 5 | App Store, Google Play, APK, RuStore, Galaxy Store |
| `appCashWay` | 3 | iap / iaa / iaa_iap |
| `videoTime` | 5 | buckets, not seconds |
| `materialRatio` | 15 | grouped Horizontal / Vertical / Square |
| `job.order` | 9 | includes the ascending variants (`max_dt`, `cnt_dt`) |

Plus four top-level blocks:

* **`flags`** — 11 single-value toggles (`isNew`, `resolution`, `asr`,
  `isViolation`, `hasPpid`, `postpage`, `hasCooperate`, `isCreative`, `isPre`,
  `isNewAd`, `singleArea`). Send `1` to enable; absent means no constraint.
* **`search_dsl_keys`** — the six `searchDsl` keys with the comparison types
  each accepts: `appid` (equal/notEqual), `campaign_name`, `slogan`, `asr`,
  `brands_name`, `developer_name` (contain/exclude). Multiple terms are joined
  with `/` inside one `value`, up to five.
* **`material_types`** — the full display map for `material.type`. Our older
  guidance said "102 image, 202 video", which mislabels 201 Video, 203
  Fullscreen Video, 103 Animated Image, 104 Multiple Image, 105 Carousel and
  301 Html.
* **`url_only_params`** — keys that live in the web UI's URL but are **not**
  GraphQL variables, so putting them in `filters` does nothing: no error, no
  effect. `daterange` is the one you will reach for; it is UI sugar for a
  relative window, so send `startDate`/`endDate` instead. `advanced` is the
  URL's name for `searchDsl`.

`media` options also carry a `category` (Social Media / Ad Networks / Local
Media) for a sectioned picker, and the endpoint serves the UI's one-click
groups as `presets`: **Meta Ads** `[2,1,10,16,32]`, **Google Ads**
`[4,11,21]`, **TikTok for Business** `[13,23,18]`. Those are worth mirroring —
a user reaching for "Meta Ads" should not have to know it means five codes.

### `GET /ad-scraper/dimensions?kind=area`

Feeds the filter dropdowns. Returns `{kind, code, name, icon, parent_code,
material_count}` per value, most-used first.

`kind` must be one of `media`, `channel`, `area`, `platform`, `format`,
`resource_element` — anything else is a 400. **Facet vocabularies are
discovered from ingested data, not seeded**: the platform doesn't publish
them anywhere we can read, so a fresh database returns an empty list. Build
the filter UI from this endpoint rather than hardcoding a country list, and
handle the empty case as "nothing ingested yet".

---

## 5. Showing the media — always presign

The bucket is private. `<video src>` against an S3 key does not work.

### `GET /ad-scraper/materials/{material_id}/media-url?kind=media&ttl=3600`

```json
{
  "material_id": "fa090010e669be22bff7ebc783c19cae",
  "kind": "media",
  "s3_key": "ad-scraper/materials/fa09.../fa09....mp4",
  "url": "http://…?X-Amz-Signature=…",
  "expires_in": 3600
}
```

* `kind` — `media` (the video/image) or `poster` (the thumbnail).
* `ttl` — seconds, 0 uses the server default. Hand a short TTL to a grid
  thumbnail and a longer one to a full-screen player.
* **404 means never mirrored.** The `detail` includes the source
  `expires_at`, so you can distinguish "we can still re-fetch this" from
  "these bytes are gone for good". Fall back to `media_url` only when
  `media_url_expires_at` is in the future.
* 503 when S3 isn't configured on the backend.

Practical grid: request `kind=poster` with a short TTL for the tiles, and
fetch `kind=media` on click. Presigned URLs are not cacheable across
sessions — don't persist them in panel state longer than `expires_in`.

Image creatives (`type: 102`) have no separate poster; `media` and `poster`
resolve to the same object.

---

## 6. Advertisers

### `GET /ad-scraper/advertisers?kind=AppBrand&search=drama`

`{id, kind, type, name, icon, alias, gp_app_url, ios_app_url, developer_*,
material_count}`, busiest first. `kind` ∈ `App` · `AppBrand` · `Website` ·
`Playlet` · `Novel` (it comes from GraphQL `__typename`, so it is
authoritative; null only if the platform stops sending it).

`alias` is a **string array** — every localised store name, up to ten. Not a
single string.

### `GET /ad-scraper/advertisers/{advertiser_id}/materials`

Every creative this advertiser runs. Same `sort`/`limit`/`offset` as
`/materials`.

**Two different app ids exist and confusing them is the main failure mode
here:**

| | Looks like | Used for |
| - | - | - |
| Advertiser id | `M3OgFwcr4yLVdauQFomkHA==` | our own filters — `advertiser_id`, and the upstream `campaign` variable |
| Store app id | `1661308505` | the **`app_id`** field when creating a job (§ 7) |

To go from a store id to an advertiser, match it inside `ios_app_url` /
`gp_app_url` — appid `1661308505` belongs to *ChatOn AI - Chat Bot
Assistant*, whose `ios_app_url` ends `/id1661308505`.

---

## 7. Starting an ingestion job

### `POST /ad-scraper/jobs`

```json
{
  "app_id": "1661308505",
  "filters": {
    "purpose": 2,
    "media": [1],
    "area": ["US", "TR", "SA"],
    "keyword": "chaton",
    "startDate": "2026-02-20",
    "endDate": "2026-08-18",
    "field": "all",
    "accurateSearch": 1
  },
  "page_from": 1,
  "page_to": 15,
  "order": "impression_inc_2y_desc"
}
```

**`filters` is the upstream GraphQL `variables` object, passed through
verbatim.** 57 variables are accepted, so an operator can build a query in
the AppGrowing web UI, copy the variables out of its network tab, and paste
them in. The panel does not need to model them all — a form covering
`purpose`, `media`, `area`, `keyword`, `startDate`/`endDate` plus a raw-JSON
escape hatch covers real use.

### The `filters` contract

`filters` is the upstream GraphQL `variables` object, forwarded verbatim.
**`purpose` no longer needs a control.** It is required upstream, but the
service fills in its configured default (`AD_DEFAULT_PURPOSE`, currently 2)
whenever a job omits it — `GET /filters` reports that value as
`filters[].default` with `required: false`. Send it only to override.
Everything else, `field` and `accurateSearch` included, is optional despite
appearing in every example copied out of the web UI's network tab.

`purpose` is the product's **top tab**, read out of the web app's own source:
`{"Game":1,"App":2,"Website":3,"Ec":4,"Account":5,"Domestic":6}`. Only 1-3 are
usable here — 4 and 5 are refused with `00:401001` and 6 answers zero rows.

**The panel does not need a control for it.** The service fills in
`AD_DEFAULT_PURPOSE` (2 = App) when a job omits the key, and `GET /filters`
reports that as `default` with `required: false`. Send it only to override —
1 for game advertisers, 3 for websites.

Two knock-on effects: under Website the UI narrows its network list to 18
(game networks are hidden), and `format` 302 Rewarded / 401 Playable exist
only under purpose 1-4.

Advertiser mix agrees (95% / 91% AppBrand under 1 and 2, flipping to 62%
Website under 3) but those are counted from the newest 400 rows of a live
feed and they move: an earlier sample of the same corpora gave 18% / 42% /
57%. Trust the direction, not the magnitude — and prefer a filter's reported
`total` over anything counted off a page.

Network density still shifts with purpose even though `media` code *validity*
does not: `media: [32]` (Threads) is valid under all three and answers 5.7M /
60.4M / 111.9M, so the Meta family is ~20x denser under 3.

**Two `media` behaviours that generate bug reports.**

* A row lists its creative's **whole** network set, not the intersection with
  your filter. Filter `media: [13]` and the rows come back TikTok — and also
  Pangle, Instagram, Facebook, because one creative runs on several networks.
  If the table filters by TikTok and then renders `media[]`, it will look
  broken. Consider highlighting the networks the user selected.
* A small network **disappears inside a big filter**. `media:
  [4,11,21,2,1,10,16,32]` reports 194 178 669 rows; adding TikTok (13) makes
  it 198 422 085 — 2.1% — and page 1 under `max_dt_desc` had zero TikTok
  rows. To surface one network, filter to it alone. `media: [13]` on its own
  returns 4 244 227 rows and a full page of TikTok.

**Strict fields — one bad value fails the WHOLE request.** The upstream never
ignores a bad code; it answers *"Parameter error, please clear the filter and
refresh"* with no indication which field was wrong. `POST /jobs` therefore
validates these up front and returns a 422 naming the problem:

| Field | Format | Rejected examples |
| - | - | - |
| `area` | **uppercase ISO-3166-1 alpha-2, one code per array element** | `"tr"` (lowercase) · `"TUR"` (ISO-3) · `""` · **`"TR;SA"` / `"TR,SA"` (two codes joined into one element)** |
| `media`, `platform`, `format`, `creativeType`, `resourceElement`, `category` | array of positive integers | `["13"]` (string) · `[0]` · unknown codes (forwarded, and the upstream rejects them) |

That `"TR;SA"` row is not hypothetical — a panel whose country multi-select
string-joins its value produces exactly it, and the upstream error gives no
hint. Send `["US", "TR", "SA"]`.

Valid code values come from `GET /dimensions?kind=…`; they are discovered from
ingested data, so a fresh database lists none. For reference, `media` spans
1-19, 21-23, 25-26, 28-34 today (20, 24, 27, 35, 36 are rejected).

**Dates.** `isAllDate: 1` **overrides** `startDate`/`endDate` — sending both
is accepted upstream and silently ignores the range (measured: identical
totals either way). `POST /jobs` refuses the combination rather than let a
date filter vanish. An inverted range (`startDate > endDate`) is also accepted
upstream and returns a nonsense subset, so it is refused too. Dates are
`YYYY-MM-DD`; any other shape fails upstream validation.

With neither `isAllDate` nor a range you get the upstream's own default recent
window, which is much smaller than the full corpus (18M vs 205M on
`purpose: 2`) — so omitting both is a filter, not "everything".

**Lenient fields — a bad value silently returns nothing.** No validation can
help here; treat an empty result as possibly-your-filter:

| Field | Behaviour |
| - | - |
| `gender` | an unknown code returns `total: 0` rather than erroring (observed values 1, 2, 3) |
| `keyword` | free text; combine with `field` (`"all"`) and `accurateSearch` (`1`) |
| `campaign` | a numeric store id returns `total: 0` — use the `app_id` field instead (`POST /jobs` refuses a numeric `campaign` for this reason) |

`field` and `accurateSearch` are **optional**, despite appearing in every
example copied out of the web UI's network tab.

A minimal working filter:

```json
{"purpose": 2}
```

A realistic one:

```json
{
  "purpose": 2,
  "media": [13],
  "area": ["TR", "US"],
  "startDate": "2026-02-20",
  "endDate": "2026-08-18",
  "field": "all",
  "accurateSearch": 1
}
```

Fields the panel owns:

| Field | Notes |
| - | - |
| `app_id` | Store app id. Compiled into the `searchDsl` clause the platform actually filters on. **Do not put an app id in `filters.campaign`** — see below |
| `page_from` / `page_to` | 1-based, inclusive. `page_to` defaults to 5 |
| `order` | `max_dt_desc` (newest) or `impression_inc_2y_desc` (best performing). Not enforced — the platform adds sorts without notice |
| `mirror` | Tri-state. Omit to follow the server policy; `false` opts this job out of mirroring; `true` opts in under a `job` policy |
| `max_attempts` | 1–10, default 3 |

**422s the panel should surface as form errors, not toasts:**

* `page_to > 200` — the upstream caps `page` at 200 with a server-fixed page
  size of 50, i.e. **10 000 rows per filter set, ever**. The message says to
  narrow the filter.
* A numeric `filters.campaign` — `campaign` takes the *advertiser* id, and
  handed a store id the upstream returns **zero rows with no error**. The
  request is refused precisely because a silent empty result reads like "this
  app has never advertised".
* `page` or `order` inside `filters` — those live in the top-level fields;
  two sources of truth for a page number is a silent-wrong-answer bug.
* `page_to < page_from`.

### Watching a job

`GET /ad-scraper/jobs?status=running&limit=50` and
`GET /ad-scraper/jobs/{job_id}`. Poll the single job every **3–5 s** while
`queued`/`running`; a job that mirrors 150 videos runs for minutes.

`status` ∈ `queued` · `running` · `succeeded` · `failed` · `cancelled`.

`POST /ad-scraper/jobs/{job_id}/cancel` (queued only — a terminal job comes
back unchanged, not an error) and `.../retry` (resets the attempt budget).

### Reading `stats`

Null until the job finishes. Then:

```json
{
  "pages_fetched": 3,
  "materials_seen": 150,
  "materials_new": 150,
  "materials_updated": 0,
  "materials_skipped": 0,
  "advertiser_edges": 154,
  "dimension_edges": 7280,
  "mirrored": 150,
  "mirror_cached": 0,
  "mirror_failed": 0,
  "mirror_skipped": 0,
  "mirror_seconds": 3.74,
  "throttle_wait_seconds": 4.57,
  "total_reported": 731,
  "truncated": true,
  "notes": ["filter set reports 731 rows; this job's window covers 150. Raise page_to (max 200) to reach the rest."]
}
```

**`truncated: true` deserves a visible warning, not a hidden field.** It
means the filter matched more than the page window could return — the job
succeeded but did not ingest everything. `notes[0]` is written for an
operator to read; render it verbatim. Either raise `page_to`, or partition
the filter (date window, country, media) across several jobs.

`materials_repeated` counts rows the API re-served within the job: past the
end of a result set it repeats the last page instead of returning empty, so
a `page_to` far above what the filter justifies is harmless but visible.
`materials_seen` counts distinct creatives only.

**Two timing fields, and the difference decides what to tell the operator.**
`mirror_seconds` is time spent downloading media; `throttle_wait_seconds` is
time held at the upstream rate gate on purpose. A job that took four minutes
with `mirror_seconds: 230` was busy, not stuck — downloading is the slow half
by a wide margin (4.46s per creative measured, and a page holds 50). A job
with a large `throttle_wait_seconds` was being paced deliberately. Neither is
a fault; showing the raw duration alone makes both look like one.

Downloads run `AD_MIRROR_CONCURRENCY` at a time (4 by default). Worth setting
expectations if you surface an ETA: that buys about 1.2-1.6x, not 4x, because
the constraint is bandwidth — measured 2.63 MB/s at 1 concurrent, 3.80 at 4,
2.80 at 8.

`mirror_cached` counts creatives already in our bucket, skipped without a
download. On a re-run of the same filter expect
`materials_new: 0, materials_updated: N, mirror_cached: N` — that is healthy
idempotency, not a no-op.

---

## 8. The session token — surface it, it expires weekly

ad_scraper authenticates upstream with one cookie value that lives ~7 days.
When it dies, **every new job fails** while already-ingested data keeps
serving fine. The panel is the only place an operator will notice.

### `GET /ad-scraper/credentials`

```json
{
  "label": "default",
  "status": "active",
  "has_session": true,
  "session_expires_at": "2026-08-25T07:28:35Z",
  "expires_in_seconds": 603447,
  "needs_refresh": false,
  "last_ok_at": "2026-08-18T11:20:04Z",
  "consecutive_failures": 0,
  "last_error": null
}
```

`status` ∈ `active` · `expired` · `login_failed` (rejected too many times —
stop-the-line) · `disabled`. The token is never returned.

`expires_in_seconds` **goes negative** once dead rather than clamping to
zero, so you can render "expired 3 days ago". Show a banner when
`needs_refresh` is true or `status != "active"`.

### `PUT /ad-scraper/credentials/session`

```json
{ "session_cookie": "eyJ0eXAiOiJKV1QiLCJhbGciOiJFUzI1NiJ9..." }
```

The operator logs into appgrowing in a browser, copies the `sessionId`
cookie, pastes it here. Expiry is derived from the token itself — do not ask
for it. A textarea plus a "how to get this" hint is the whole UI.

`session_expires_at: null` in the response means the expiry could not be
parsed. The token may still work, but no advance warning is possible.

**There is no login endpoint.** Automatic login was considered and dropped;
nothing stores a password. Don't build a username/password form.

**Rotating a token takes effect immediately**, in the worker as well as the
API — there is no cache to flush, so the panel needs no "apply" step after a
PUT. (`POST /credentials/session/invalidate-cache` used to exist for that and
has been removed; it only ever cleared the API process's copy, which was the
half that did not matter.)

`POST /ad-scraper/credentials/disable` exists for edge cases — not worth
surfacing in v1.

### `GET /ad-scraper/health` and `/ready`

`/health` is a Docker liveness probe — it appears in the generated types but
there is nothing for the panel to do with it. `/ready` is the useful one:

### `GET /ad-scraper/ready`

```json
{"status": "ready", "checks": {"database": true}, "youcloud_session": "active"}
```

`youcloud_session` ∈ `active` · `expiring` · `missing` · `locked_out`. A dead
token does **not** make `/ready` fail — the read API is still fine. Use this
field for a status pill, not for a health check.

---

## 9. Suggested information architecture

```
Ad Intelligence
├── Creatives            grid, poster thumbnails, video on click
│                        filters: type · country · media · platform
│                                 min impressions · min days on air · has transcript
│                        sort: impressions · days on air · newest
├── Creative detail      player, transcript, advertisers, facets, run window
├── Advertisers          list w/ creative counts → click through to their creatives
└── Ingestion
    ├── New job          app_id + filter form + page window (+ raw JSON escape hatch)
    ├── Job list         status, stats, truncation warnings
    └── Session token    status pill + paste box
```

The grid is the point of the whole feature — ad intelligence means *looking
at* the creatives. Lead with poster thumbnails and inline video, not a table
of ids.

---

## 10. Pitfalls

1. **`run_days` is days on air, `media_duration_sec` is seconds.** Swapping
   them yields a card claiming a 209-second video that ran for 26 days.
2. **Render `impression_inc_2y_raw`, sort on `impression_inc_2y`.** The
   platform's top bucket is literally `">10M"`.
3. **Always presign.** An S3 key is not a URL; `media_url` may already be dead.
4. **`app_id` for a store id, `advertiser_id` for the opaque one.** A numeric
   `filters.campaign` is refused for you; a numeric `advertiser_id` just
   returns nothing.
5. **Facets AND across kinds.** `media=2&area=TR` is an intersection, and an
   empty result is often an over-constrained filter rather than missing data.
6. **`truncated: true` on a succeeded job** means partial ingestion. Show it.
7. **10 000 rows is a hard ceiling per filter set.** Not a paging limit you
   can raise — the filter has to be partitioned.
8. **A re-run reporting `materials_new: 0` is correct**, not a failure.
9. **A dead session token fails jobs while reads keep working.** Don't gate
   the whole section on `youcloud_session`.
10. **Facet vocabularies come from ingested data.** A fresh database has no
    countries to filter by.
11. **List endpoints return bare arrays** — no `{items, total}` envelope, and
    no total count anywhere.
12. **Global admin only.** 403 for members; hide the section rather than
    letting them hit it.

---

## 11. Not exposed (don't build)

* No creative editing — the data is a read-only mirror of a third-party
  source. `PATCH /materials/{id}` does not exist.
* No delete. Retention/pruning is a backend concern.
* No metric history — impressions over time is not queryable; only the
  latest value per creative is stored.
* No login form (§ 8).
* No per-project scoping — ad_scraper is global.
* No webhooks or SSE. Poll job status.

---

## 12. Quick links

| What | Where |
| - | - |
| Federated OpenAPI (type generation) | `http://localhost:8000/api/v1/openapi.json` |
| ad_scraper Swagger (direct, dev only) | `http://localhost:8083/docs` |
| Operator guide | `services/ad_scraper/README.md` |
| Backend context / decisions | `services/ad_scraper/CLAUDE.md` |

The direct port is published only by `docker-compose.override.yml` and bound
to `127.0.0.1`. In production the gateway is the only reachable surface, so
build against `:8000` from the start.
