# content_pipeline — Admin Panel API Guide

> **For the admin panel project at `/Users/batinduz/htdocs/baley/agent-platform-admin`.**
>
> Read this **before** wiring API calls. It's the single source of truth for what
> the panel can talk to and how. The OpenAPI schema is the typed contract; this
> file is the *operational* contract — flows, polling, gotchas.

---

## 1. Quick start

### Base URL

| Environment | URL |
|---|---|
| Local dev (docker-compose) | `http://localhost:8082` |
| Prod | TBD (Hetzner) |

### Auth

Single static API key. Send on every request:

```
X-API-Key: <CP_API_KEY from .env>
```

Endpoints **without** the header return `401`. There is no user table, no JWT,
no per-user scoping yet — that lands in CP-M8.

### OpenAPI / typed client

The full schema lives at `GET /api/v1/openapi.json`. Generate types once at
build time:

```bash
npx openapi-typescript http://localhost:8082/api/v1/openapi.json -o src/api/types.ts
```

Re-run whenever the backend ships a new milestone. The `commit:` line in
each backend release identifies the contract version (see
`services/content_pipeline/CLAUDE.md`).

### Stack the panel will need

- Polling (every 2-5s) for in-flight operations: scenario, scene_renders,
  render_variants, publish_jobs.
- Drag-drop for the calendar (`@dnd-kit/core` or similar).
- Calendar view (`@fullcalendar/react` is fine; data shape matches).
- Direct-to-S3 PUT for asset uploads (no proxy).
- Image / video `<img>` / `<video>` against presigned GET URLs (NOT `s3_key`).

---

## 2. Conventions

### Project scoping

Almost every resource lives under `/api/v1/projects/{project_id}/...`. The
`project_id` is a UUID. Cross-project leakage is enforced server-side: a
GET for an asset that belongs to a different project returns `404`, not
`403` (we don't leak existence).

Two exceptions:
- `GET/POST /api/v1/global/model-routes` — provider/model defaults shared
  across all projects.
- `GET/POST /api/v1/projects` — project itself.

### Pagination

Everywhere lists exist:

```
?limit=50&offset=0
```

Defaults are sane; max `limit` varies by endpoint (50-1000).

### Status codes

| Code | Meaning |
|---|---|
| `200` | OK |
| `201` | Created (POSTs that produce a row) |
| `204` | Deleted |
| `401` | Missing / wrong `X-API-Key` |
| `404` | Resource doesn't exist OR isn't in the requested project |
| `409` | State machine refused the transition, OR reuse policy blocked the create. **Body has structured detail.** |
| `422` | Pydantic validation failed; body lists offending fields |

### Time

All timestamps are **ISO-8601 UTC**. The admin panel renders local time.
`posting_strategy.timezone` is the project's local zone (default
`Europe/Istanbul`); slot expressions like `"Mon 19:00"` are interpreted in
that zone, then stored as UTC `scheduled_at`.

---

## 3. Resource catalog

### 3.1 Projects

```
POST   /api/v1/projects                         create
GET    /api/v1/projects?status=&limit=&offset=  list
GET    /api/v1/projects/{pid}                   get
PATCH  /api/v1/projects/{pid}                   update (default brand_kit / social_account / reuse_policy / weekly_budget_cap_usd)
DELETE /api/v1/projects/{pid}                   soft delete (status='archived')
```

`reuse_policy ∈ {block, warn, silent}` — `warn` is the default and the
correct choice for most projects. `block` denies even with `force=true`.

### 3.2 Brand kits

```
POST   /api/v1/projects/{pid}/brand-kits
GET    /api/v1/projects/{pid}/brand-kits
GET/PATCH/DELETE /api/v1/projects/{pid}/brand-kits/{kit_id}
```

One default kit per project (server enforces). Holds `voice_id` for TTS
and `style_prompt_suffix` injected into the analyzer prompt.

### 3.3 Social accounts (publishing)

**Distinct from `ig_scraper.ig_accounts`.** These are the brand's own IG /
TikTok accounts that the publisher publishes TO.

```
POST/GET/GET/PATCH/DELETE /api/v1/projects/{pid}/social-accounts[/{id}]
```

`credentials` POST body is `{access_token, ig_user_id}` for IG (more
fields when TikTok lands in CP-M7). Stored encrypted at rest; the read
shape exposes only `has_credentials: bool`, never the secret.

### 3.4 Templates & music

Project-scoped libraries the renderer consumes. Both use the **presigned
upload pattern** (see § 5.1).

```
POST   /api/v1/projects/{pid}/templates           # outro / intro / lower_third / sticker / transition
POST   /api/v1/projects/{pid}/music-tracks
GET/PATCH/DELETE per id
```

### 3.5 References (the production pool)

```
POST  /api/v1/projects/{pid}/references/upload                   # admin-uploaded
POST  /api/v1/projects/{pid}/references/import-from-scraper      # body: {ig_post_id}
GET   /api/v1/projects/{pid}/references?status=&source_provider=
GET   /api/v1/projects/{pid}/references/{id}
PATCH /api/v1/projects/{pid}/references/{id}
POST  /api/v1/projects/{pid}/references/{id}/archive
GET   /api/v1/projects/{pid}/references/{id}/usage-check         # how many scenarios already use this?
GET   /api/v1/projects/{pid}/inbox/candidates                    # references with status='candidate'
```

The panel's "Inbox" tab maps to `inbox/candidates`. The admin reviews,
approves (PATCH `status='approved'`) or archives.

### 3.6 Intake rules

```
POST/GET/PATCH/DELETE /api/v1/projects/{pid}/intake-rules[/{id}]
```

JSONB `conditions` matches scraped/imported references. CP-M2.5 will wire
the auto-import subscriber; today rules exist but only inform manual
admin actions.

### 3.7 Scenarios — the production script

The most state-rich resource. **Read § 4 (state machines) before wiring.**

```
POST  /api/v1/projects/{pid}/scenarios
       body: {reference_id, target_variants[], quality_tier?, force?, reuse_reason?, notes?}
GET   /api/v1/projects/{pid}/scenarios?status=&limit=&offset=
GET   /api/v1/projects/{pid}/scenarios/{id}
PATCH /api/v1/projects/{pid}/scenarios/{id}                 # only in draft/pending_review
POST  /api/v1/projects/{pid}/scenarios/{id}/analyze         # re-enqueue analyzer if Redis was down
POST  /api/v1/projects/{pid}/scenarios/{id}/approve         # pending_review → approved
POST  /api/v1/projects/{pid}/scenarios/{id}/regenerate      # snapshot → previous_scenario_json, version+1, re-analyze
POST  /api/v1/projects/{pid}/scenarios/{id}/start-images    # approved → generating_images, fan-out
GET   /api/v1/projects/{pid}/scenarios/{id}/scene-renders
POST  /api/v1/projects/{pid}/scenarios/{id}/scenes/{idx}/regenerate-image
       body: {aspect_ratio?, prompt_override?}
POST  /api/v1/projects/{pid}/scenarios/{id}/start-videos    # images_ready → generating_videos
POST  /api/v1/projects/{pid}/scenarios/{id}/scenes/{idx}/regenerate-video
       body: {aspect_ratio?, motion_override?}
POST  /api/v1/projects/{pid}/scenarios/{id}/start-audio     # videos_ready → generating_audio
POST  /api/v1/projects/{pid}/scenarios/{id}/regenerate-voiceover
       body: {voice_id_override?, text_override?}
POST  /api/v1/projects/{pid}/scenarios/{id}/reselect-music
       body: {music_track_id?}                              # null → auto-pick
POST  /api/v1/projects/{pid}/scenarios/{id}/start-compose   # audio_ready → composing
GET   /api/v1/projects/{pid}/scenarios/{id}/render-variants
POST  /api/v1/projects/{pid}/scenarios/{id}/render-variants/{variant_id}/recompose
POST  /api/v1/projects/{pid}/scenarios/{id}/render-variants/{variant_id}/approve
POST  /api/v1/projects/{pid}/scenarios/{id}/approve-final   # final_pending_review → approved_final
GET   /api/v1/projects/{pid}/scenarios/{id}/generation-calls
```

### 3.8 Posting strategy + weekly plans + slots

```
GET/PUT /api/v1/projects/{pid}/posting-strategy             # lazy-created on first GET

POST    /api/v1/projects/{pid}/weekly-plans/generate        # body: {week_start, fill}
GET     /api/v1/projects/{pid}/weekly-plans
GET     /api/v1/projects/{pid}/weekly-plans/{plan_id}
GET     /api/v1/projects/{pid}/weekly-plans/{plan_id}/slots
POST    /api/v1/projects/{pid}/weekly-plans/{plan_id}/approve
POST    /api/v1/projects/{pid}/weekly-plans/{plan_id}/refill   # re-run fill_strategy

POST    /api/v1/projects/{pid}/plan-slots                  # manual create
PATCH   /api/v1/projects/{pid}/plan-slots/{slot_id}        # drag-drop edits time/account
POST    /api/v1/projects/{pid}/plan-slots/{slot_id}/assign-variant
POST    /api/v1/projects/{pid}/plan-slots/{slot_id}/skip
DELETE  /api/v1/projects/{pid}/plan-slots/{slot_id}
POST    /api/v1/projects/{pid}/plan-slots/{slot_id}/publish-now
GET     /api/v1/projects/{pid}/plan-slots/{slot_id}/publish-jobs

GET     /api/v1/projects/{pid}/stock?preset=ig_reels
GET     /api/v1/projects/{pid}/calendar?from=2026-05-11T00:00:00Z&to=2026-05-18T00:00:00Z
```

### 3.9 Media assets + cost

```
GET  /api/v1/projects/{pid}/media-assets/{asset_id}
GET  /api/v1/projects/{pid}/media-assets/{asset_id}/preview-url?ttl=3600
GET  /api/v1/projects/{pid}/media-assets/{asset_id}/history

GET  /api/v1/projects/{pid}/cost-summary?from=&to=
GET  /api/v1/projects/{pid}/scenarios/{sid}/generation-calls
```

### 3.10 Model routes

```
GET   /api/v1/projects/{pid}/model-routes
POST  /api/v1/projects/{pid}/model-routes
PATCH /api/v1/projects/{pid}/model-routes/{route_id}
DELETE ...
GET   /api/v1/global/model-routes
POST/PATCH/DELETE /api/v1/global/model-routes/{route_id}
```

The "Settings → AI providers" page lets admins swap Claude → GPT-4 →
Gemini per task without redeploying. Project-scoped rows shadow global
defaults.

---

## 4. State machines (READ THIS)

### 4.1 `scenarios.status`

```
draft
  ↓ (analyzer enqueue)
analyzing
  ↓ (LLM done)
pending_review     ← admin can PATCH scenario_json or POST /regenerate
  ↓ (admin POSTs /approve)
approved
  ↓ (admin POSTs /start-images)
generating_images   ← scene_renders fan out
  ↓ (all renders image_ready)
images_ready       ← admin can POST /scenes/{idx}/regenerate-image
  ↓ (admin POSTs /start-videos)
generating_videos
  ↓
videos_ready       ← admin can POST /scenes/{idx}/regenerate-video
  ↓ (admin POSTs /start-audio)
generating_audio    ← TTS + auto music pick
  ↓
audio_ready        ← admin can /regenerate-voiceover or /reselect-music
  ↓ (admin POSTs /start-compose)
composing          ← render_variants fan out (ffmpeg)
  ↓ (all variants ready)
final_pending_review ← admin can /render-variants/{vid}/recompose
  ↓ (admin POSTs /approve-final)
approved_final     ← variant assets land in stock pool
```

Failure path: any state → `failed` (with `last_error` text). Recovery:
`POST /scenarios/{id}/regenerate` resets to `analyzing`.

The panel renders this as a **stepper** with each transition exposed as a
button. Disabled states should be visually clear (greyed out, with the
state name as tooltip).

### 4.2 `scene_renders.status`

```
pending → generating_image → image_ready
                          → generating_video → video_ready
                          → failed
```

Per-scene per-aspect (e.g. `(scenario_id, scene_idx=0, aspect_ratio="9:16")`).
The grid view is **scenes × aspect_groups**, with status badges per cell.

### 4.3 `render_variants.status`

```
pending → composing → ready → approved → published
                  → failed
```

One per `(scenario_id, preset_key)`. The "final review" tab shows each
preset's video player + approve button.

### 4.4 `plan_slots.status`

```
empty → filling → ready → scheduled → publishing → published
                                                → failed
                                       → skipped
```

Calendar cells get a colored dot per status. `empty` slots that have
`suggested_variant_ids` get a "Pick one" overlay (auto_suggest mode).

### 4.5 `publish_jobs.status`

```
pending → uploading → processing → published
                                → failed
```

Drill-down only — when admin clicks a slot to inspect publish history.

---

## 5. Special patterns

### 5.1 Presigned upload (any asset)

The API **never proxies bytes**. Admin uploads go directly browser → S3.

```
1. POST /api/v1/projects/{pid}/assets/upload-url
   body: {kind: "brand_logo" | "template_video" | "music" | "reference_media" | "misc",
          filename: "logo.png", content_type?: "image/png"}
   → {upload_url, s3_key, expires_in, method: "PUT", required_headers: {...}}

2. PUT <upload_url> with the bytes
   Headers: Content-Type matching what you sent in step 1

3. Patch the relevant resource with the returned s3_key:
   PATCH /api/v1/projects/{pid}/brand-kits/{kit_id}
   body: {logo_s3_key: "<s3_key from step 1>"}
```

In dev with MinIO, the presigned URL points at `http://minio:9000/...`.
That's reachable from the API container but not from a browser running
on the host. **Workaround**: have the dev panel run inside docker
network, OR set `S3_ENDPOINT=http://localhost:9000` in `.env` (the API
won't reach MinIO that way, so don't run worker jobs in that mode). Prod
with Hetzner has no such issue — endpoint is publicly reachable.

### 5.2 Preview URL (rendering images / videos)

`media_assets.s3_key` is **NOT a URL**. Browsers can't fetch it. Always go
through:

```
GET /api/v1/projects/{pid}/media-assets/{asset_id}/preview-url?ttl=3600
→ {asset_id, s3_key, preview_url, expires_in}
```

Use `preview_url` in `<img src=...>` / `<video src=...>`. Cache the
response for `expires_in - 60` seconds to avoid re-fetching on every
render.

For thumbnails, use a short TTL (e.g. `?ttl=300`); for full-screen
preview, longer (`?ttl=3600`).

### 5.3 Asset version history / rollback

Every regenerate (scene image, scene video, voiceover, variant
recompose) creates a NEW `media_assets` row. The chain links via
`previous_version_id` / `replaced_by_id`.

```
GET /api/v1/projects/{pid}/media-assets/{asset_id}/history
→ {asset_id, versions: [v1, v2, v3, ...], current_version: 3}
```

Pass any version id (root, intermediate, or active); the response is the
full chain oldest → newest. Render as a strip of thumbnails with the
active version highlighted.

**Rollback** isn't exposed yet. Today the chain is read-only history. If
admins need to revert, CP-M8 can add `POST /media-assets/{old_id}/promote`
that swaps `replaced_by_id` links. Flag it if you hit a real need.

### 5.4 Polling

In-flight operations have no WebSocket / SSE today (CP-M8). Poll:

| Resource | Interval | Stop when |
|---|---|---|
| `scenario.status` | 2s | terminal: `pending_review`, `images_ready`, `videos_ready`, `audio_ready`, `final_pending_review`, `approved_final`, `failed` |
| `scene-renders` (entire list) | 2s | every row in `image_ready` / `video_ready` / `failed` |
| `render-variants` (entire list) | 3s | every row in `ready` / `approved` / `failed` |
| `publish-jobs` for a slot | 5s | latest in `published` / `failed` |
| Cost summary | on-demand | — |
| Calendar | on-demand + after every slot edit | — |

Use TanStack Query's `refetchInterval` with the stop condition. Don't
poll if the tab is hidden (`document.visibilityState`).

### 5.5 Reuse policy (creating scenarios)

```
POST /api/v1/projects/{pid}/scenarios
body: {reference_id, target_variants: ["ig_reels"], force: false}

# If reference was used before AND project.reuse_policy != 'silent':
→ 409
   {detail: {
     error: "reference_already_used",
     previously_used: true,
     usage_count: 2,
     last_used_days_ago: 14,
     previous_scenarios: [{scenario_id, status, created_at, reuse_reason}],
     project_reuse_policy: "warn",
     hint: "set force=true and supply reuse_reason to override"
   }}
```

UX: open a modal showing previous_scenarios with thumbnails, ask the
admin to confirm + supply `reuse_reason` (audit trail). Resend with
`force: true, reuse_reason: "..."`.

For `reuse_policy: "block"` projects, even `force: true` returns 409.
Show a hint: "this project's policy blocks reuse; switch to 'warn' if
needed."

### 5.6 Slot auto_suggest mode (default)

When `posting_strategy.fill_strategy === "auto_suggest"`:

- `weekly-plans/generate` materializes empty slots.
- For each empty slot, the planner writes top-3 stock candidates into
  `plan_slots.suggested_variant_ids` (UUIDs).
- The panel renders the slot as **empty with "3 suggestions"** badge.
  Click → modal showing the 3 variants (preview URL + approve button).
  Click one → `POST /plan-slots/{id}/assign-variant {variant_id}`.

When `fill_strategy === "auto_fill"`:
- Slots are auto-populated; admin only intervenes to swap.

When `manual`: admin builds the timeline themselves; suggested_variant_ids stays empty.

### 5.7 Drag-drop on the calendar

The calendar fetches via `GET /calendar?from=&to=`. Each slot is a draggable
event. On drop:

```
PATCH /api/v1/projects/{pid}/plan-slots/{slot_id}
body: {scheduled_at: "2026-05-13T16:00:00Z"}
```

Optimistic update on the client: move the event immediately, rollback on
non-2xx. The backend never validates "slot conflicts with another slot" —
that's the admin's call (multiple posts at the same time are valid for
multi-account projects).

### 5.8 IG public URL (prod gotcha)

The publish worker hands `media_assets.s3_key` to the IG Graph API as a
public URL. It first tries `s3.public_url(s3_key)` (works when the bucket
is public). If that fails, it falls back to a 24h presigned GET.

For prod with Hetzner: configure the bucket as **public-read on `finals/`
prefix** so Meta's CDN can pull without auth.

For dev with MinIO + localhost: IG can't reach you. Expect publish jobs
to fail with "video URL unreachable" until the project moves to prod.
Don't surface this as a panel error — show a banner: "Publishing requires
prod deployment."

---

## 6. End-to-end flows

### 6.1 First-time project setup

```
1. POST /projects                        → {id: "p1"}
2. POST /projects/p1/brand-kits          → upload logo via /assets/upload-url, PATCH s3_key
3. POST /projects/p1/social-accounts     → IG access_token + ig_user_id
4. PATCH /projects/p1                    → {default_brand_kit_id, default_social_account_id}
5. PUT  /projects/p1/posting-strategy    → tweak weekly_quota / preferred_slots if needed
6. (Optional) POST /projects/p1/music-tracks  → upload 5-10 mp3s for the library
```

### 6.2 Reference → published video

```
1. POST /projects/p1/references/upload (or /import-from-scraper)
   → {id: "r1", status: "candidate"}

2. PATCH /projects/p1/references/r1 {status: "approved"}

3. GET /projects/p1/references/r1/usage-check  → {previously_used: false}

4. POST /projects/p1/scenarios
   {reference_id: "r1", target_variants: ["ig_reels", "tiktok"]}
   → {id: "s1", status: "draft", ...}
   (analyzer auto-enqueued)

5. Poll GET /projects/p1/scenarios/s1 every 2s → status: "pending_review"

6. (Admin reviews scenario_json, optionally PATCHes)

7. POST /projects/p1/scenarios/s1/approve

8. POST /projects/p1/scenarios/s1/start-images
   Poll GET /projects/p1/scenarios/s1/scene-renders every 2s
   (admin can /scenes/{idx}/regenerate-image any cell)

9. POST /projects/p1/scenarios/s1/start-videos
   Poll same; regenerate-video same.

10. POST /projects/p1/scenarios/s1/start-audio
    Poll. (Admin can /regenerate-voiceover or /reselect-music.)

11. POST /projects/p1/scenarios/s1/start-compose
    Poll GET /render-variants. Recompose any failed/dissatisfied variant.

12. POST /projects/p1/scenarios/s1/render-variants/{vid}/approve
    (each variant separately)

13. POST /projects/p1/scenarios/s1/approve-final

14. POST /projects/p1/weekly-plans/generate {week_start: "2026-05-11"}
    → 28 empty slots created (or with suggestions if auto_suggest)

15. (Admin clicks an empty slot → picks the variant → /assign-variant)

16. (Slot's scheduled_at hits; publisher poller enqueues automatically.)

17. GET /projects/p1/plan-slots/{slot_id}/publish-jobs
    → {status: "published", provider_media_id: "..."}
```

### 6.3 Cost dashboard

```
GET /projects/p1/cost-summary?from=2026-05-01T00:00:00Z&to=2026-05-09T00:00:00Z
→ {
    total_cost_usd: 14.32,
    success_calls: 87,
    failed_calls: 3,
    by_task: [{task_key:"scene_image", call_count:42, cost_usd:1.05}, ...],
    by_provider: [{provider:"openrouter", model_id:"...", call_count:8, cost_usd:0.61}, ...],
    weekly_budget_cap_usd: 50.00,
    weekly_budget_remaining_usd: 35.68
  }
```

Drill-down for a specific scenario:

```
GET /projects/p1/scenarios/s1/generation-calls
→ [{task_key, provider, model_id, cost_usd, latency_ms, status, ...}, ...]
```

---

## 7. Suggested information architecture

A possible navigation map for the panel; not prescriptive, but reflects
how the data is shaped:

```
[Project switcher in sidebar — affects everything below]

📥 Inbox            → /inbox/candidates (references awaiting decision)
📚 References       → /references (full library, filters by status / source)
🎬 Scenarios        → /scenarios (kanban by status OR table)
   └ Detail page    → state-machine stepper + scenario_json editor +
                      scene grid + audio panel + variants gallery +
                      cost drilldown tab
📅 Plan             → /weekly-plans (calendar week view + slot drawer)
📦 Stock            → /stock (approved variants, filterable by preset)
📊 Cost             → /cost (summary + provider/task breakdown)
⚙ Settings         → /settings
   ├ Brand kits
   ├ Social accounts (publish)
   ├ Templates
   ├ Music library
   ├ Posting strategy
   ├ Intake rules
   └ AI providers (model_routes)
```

---

## 8. Pitfalls / gotchas

1. **Don't display `s3_key` directly.** Always go through `/preview-url`.
   Browsers can't render private S3 keys.

2. **Don't poll a hidden tab.** Use `document.visibilityState`. Saves cost
   on the API and keeps the user's bandwidth quiet.

3. **Versioned regenerate is one-shot.** A scene image regenerate produces
   a new version; the next regenerate replaces THAT, not the original.
   `/history` shows the full chain.

4. **State transitions are strict.** You can't `start-videos` from
   `draft`. The API returns `409` with a clear `detail`. Surface it.

5. **`weekly-plans/generate` is idempotent.** Re-clicking won't duplicate
   slots; it inserts only missing tuples. Safe to call from a "Refill"
   button.

6. **Reuse policy can `block` even with `force: true`.** Read
   `project_reuse_policy` from the 409 body and adapt UX.

7. **`presigned_get_url` TTL** maxes at 86400s (24h). Don't try to render
   long-lived dashboard tiles with one-shot URLs — fetch fresh ones.

8. **The publisher needs publicly-fetchable URLs.** In dev (MinIO +
   localhost), `publish-now` will surface unreachable-URL errors. Show a
   "Publishing only works in prod" banner in dev mode.

9. **Captions / hashtags** aren't on `plan_slots` yet (CP-M6.5). For now,
   the IG publisher ships an empty caption. Plan UI for it but don't make
   it required.

10. **`scenario.target_variants` after creation** drives the fan-out.
    Changing it post-`approved` is **not supported** today. Surface a
    "lock" once the scenario passes `approved`.

---

## 9. What's NOT exposed yet (don't build these flows)

- Multi-image carousel feed posts — single asset only.
- Webhook receivers for Meta / TikTok — we poll inside the worker.
- Real-time WebSocket / SSE — polling only.
- User-level auth — single static API key for now.
- Per-slot caption / hashtag override — coming in CP-M6.5 (today the publisher
  ships an empty caption / title).
- TikTok scraper / `tt_videos` import-from-scraper — not in ig_scraper yet.

## 9.5 Auto-generation rules (CP-M7)

Proactive scenario generation independent of plan slots. Useful when admins
want a steady drip of new content without manually picking references.

```
POST   /api/v1/projects/{pid}/auto-generation-rules
       body: {name, enabled?, pick_strategy?, daily_quota?, target_variants?,
              quality_tier?, budget_cap_usd?}
GET    /api/v1/projects/{pid}/auto-generation-rules
GET    /api/v1/projects/{pid}/auto-generation-rules/{rule_id}
PATCH  /api/v1/projects/{pid}/auto-generation-rules/{rule_id}
DELETE /api/v1/projects/{pid}/auto-generation-rules/{rule_id}
POST   /api/v1/projects/{pid}/auto-generation-rules/{rule_id}/run-now
```

`pick_strategy ∈ {highest_score, newest, diverse}`. `daily_quota` caps how
many scenarios this rule can create across 24h. `budget_cap_usd` is a
**weekly** cap (ISO Monday 00:00 UTC); the project's
`weekly_budget_cap_usd` also applies — the more restrictive wins.

**`POST /run-now`** bypasses the hourly cron and tries to spawn one
scenario immediately. Returns:

```json
{"rule_id": "...", "spawned_scenario_id": "...", "reason": null}
```

or, when nothing was eligible:

```json
{"rule_id": "...", "spawned_scenario_id": null,
 "reason": "rule disabled, daily_quota reached, budget exhausted, or no candidate references"}
```

The hourly auto-gen loop in the scheduler walks every enabled rule across
every active project; the rule's `last_run_at` lets the panel show "next
run in X minutes" countdown.

Auto-gen scenarios are tagged with `scenario.created_by="auto_gen:{rule_id}"`
— useful for filtering on the scenarios list.

## 9.6 TikTok publishing (CP-M7)

`social_accounts` already supported `provider="tiktok"`. The publisher is now
wired. Credentials shape: `{"access_token": "...", "open_id": "..."}`.

The same `POST /plan-slots/{id}/publish-now` endpoint dispatches to TikTok
when the slot's social_account has `provider="tiktok"`. Same polling cadence
on `/publish-jobs`. Provider-specific media_id extraction returns TikTok's
`publicaly_available_post_id` (yes — that's TikTok's typo, not ours).

---

## 10. Quick links

- OpenAPI: `GET /api/v1/openapi.json` (Swagger UI at `/docs`)
- Health: `GET /health` (returns `{status: "ok"}` immediately)
- Readiness: `GET /ready` (verifies Postgres connectivity)
- Metrics: `GET /metrics` (Prometheus exposition)
- This service's design doc: `services/content_pipeline/PLAN.md`
- Status / completed milestones: `services/content_pipeline/CLAUDE.md`

---

## 11. Contact / escalation

If an endpoint behaves unexpectedly:

1. Check `CLAUDE.md` for the milestone it belongs to + the "What's
   deliberately stubbed" note.
2. Cross-reference with `PLAN.md` § 6 (API surface).
3. If the contract truly drifted from this doc, file a backend issue with
   the request/response pair.
