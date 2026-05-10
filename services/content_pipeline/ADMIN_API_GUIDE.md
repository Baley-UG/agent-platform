# Admin Panel API Guide

> **For the admin panel project at `/Users/batinduz/htdocs/baley/agent-platform-admin`.**
>
> Read this **before** wiring API calls. The OpenAPI schema is the typed
> contract; this file is the *operational* contract — flows, polling, gotchas.

> Authoritative location: `services/content_pipeline/ADMIN_API_GUIDE.md`
> in the agent-platform repo. Pulling this file periodically is enough —
> the OpenAPI schema gives you everything else.

---

## 1. Architecture (CP-M9)

The admin panel talks to **only one API** — the main `app/` service:

```
┌──────────────┐    HTTPS    ┌──────────────────────────────┐
│ Admin Panel  │ ──────────▶ │  Main app (port 8000)        │
│              │             │  • Bearer JWT auth           │
│              │             │  • users + project_memberships│
│              │             │  • generic gateway proxy     │
└──────────────┘             └────────┬─────────────────────┘
                                      │  (internal, X-API-Key)
                          ┌───────────┴────────────┐
                          ▼                        ▼
                ┌───────────────────┐    ┌──────────────────┐
                │ content_pipeline  │    │  ig_scraper      │
                │ (port 8082)       │    │  (port 8081)     │
                └───────────────────┘    └──────────────────┘
```

- **Single base URL** for the panel: `http://localhost:8000` (dev) /
  `https://api.<your-domain>` (prod).
- **Single user system** in main `app/`. Admin panel users + chatbot
  users share the same `user` table; the admin role gates admin access.
- **Gateway proxy** at `/api/v1/cp/{path}` and `/api/v1/scraper/{path}`
  — main app validates Bearer JWT + project membership, then forwards
  the request to the downstream service with its `X-API-Key`.
- **Downstream services are not exposed publicly** (no host port mapping
  in compose by default). Only main app is reachable from the browser.

### Quick start

```bash
# Login
curl -X POST http://localhost:8000/api/v1/admin/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"admin@example.com","password":"yourpass"}'
# → {access_token, refresh_token, expires_at, user}

# Use the access token everywhere else
curl http://localhost:8000/api/v1/admin/auth/me \
  -H "Authorization: Bearer <access_token>"

# Call content_pipeline through the gateway
curl http://localhost:8000/api/v1/cp/projects \
  -H "Authorization: Bearer <access_token>"

# Call ig_scraper through the gateway (admin role required)
curl http://localhost:8000/api/v1/scraper/jobs \
  -H "Authorization: Bearer <access_token>"
```

### OpenAPI / typed client

Generate types once at build time, then regenerate after every backend
release:

```bash
npx openapi-typescript http://localhost:8000/api/v1/openapi.json -o src/api/types.ts
```

The schema includes the gateway routes (`/cp/{path}`, `/scraper/{path}`)
as catch-alls; **the actual downstream endpoints are listed in each
service's own OpenAPI** at:

- content_pipeline: `http://content-pipeline-api:8082/api/v1/openapi.json`
  (internal-only; for development read the file directly from
  `services/content_pipeline/app/api/v1/`)
- ig_scraper: similar.

Recommended: pull the downstream OpenAPI files at panel build time and
generate a richer client (your gateway prefix is constant).

---

## 2. Conventions

### Auth

```
Authorization: Bearer <access_token>
```

Login flow:

1. `POST /api/v1/admin/auth/login {email, password}` →
   `{access_token, refresh_token, expires_at, user}`
2. Store `access_token` in memory (Zustand / Redux state).
3. Store `refresh_token` in `localStorage` (or httpOnly cookie if your
   hosting allows server-side cookie setting).
4. On every request, attach `Authorization: Bearer <access_token>`.
5. On `401` response → call `POST /api/v1/admin/auth/refresh
   {refresh_token}` → got a new pair → retry the original request.
   If refresh itself fails, navigate to login.
6. On logout: `POST /api/v1/admin/auth/logout {refresh_token}` and
   clear local state.

**Token TTL**:
- Access: 1h (configurable: `ADMIN_ACCESS_TOKEN_TTL_MINUTES`)
- Refresh: 7d (configurable: `ADMIN_REFRESH_TOKEN_TTL_DAYS`)
- Refresh **rotates**: each successful refresh revokes the old token
  and issues a new one. If the previous token leaks, the first replay
  invalidates it.

**Roles**:
- Global: `admin` (full system) or `member` (only assigned projects)
- Per-project: `owner` / `editor` / `viewer` (in `project_memberships`)

A `member` without a `project_memberships` row for a project gets
`404` on `/cp/projects/{pid}/...` (we don't leak existence). `admin`
always passes.

### Project scoping

Path forms like `/cp/projects/{project_id}/...` are gated:
1. Global admin → always allowed
2. Member with any `project_memberships` row for that project → allowed
3. Anyone else → 404

The gateway extracts the UUID from the path automatically. You don't
need to pass `project_id` in any other shape.

### Pagination

`?limit=50&offset=0` on every list endpoint (defaults sane; max varies
50–1000).

### Status codes

| Code | Meaning |
|---|---|
| `200` | OK |
| `201` | Created |
| `204` | Deleted / logout |
| `401` | Missing / expired / invalid token |
| `403` | Authenticated but lacks role / membership for this action |
| `404` | Resource doesn't exist OR scope-mismatched (membership missing) |
| `409` | State machine refused / reuse policy / unique constraint |
| `422` | Pydantic validation; body lists offending fields |
| `502` | Gateway couldn't reach a downstream service |

---

## 3. Endpoint catalog

### 3.1 Admin auth (main app)

```
POST   /api/v1/admin/auth/login            {email, password}
                                            → TokenResponse
POST   /api/v1/admin/auth/refresh          {refresh_token}
                                            → TokenResponse (rotates the refresh)
POST   /api/v1/admin/auth/logout           {refresh_token}    # 204
GET    /api/v1/admin/auth/me               → AdminUserRead (with memberships)
POST   /api/v1/admin/auth/change-password  {current_password, new_password}
                                            → AdminUserRead
```

`AdminUserRead`:
```ts
{
  id: number,
  email: string,
  name: string | null,
  role: 'admin' | 'member',
  status: 'active' | 'disabled',
  last_login_at: string | null,
  created_at: string,
  memberships: [{id, project_id, role}]
}
```

### 3.2 User management (main app, **admin only**)

```
POST   /api/v1/admin/users                 {email, password, name?, role}
GET    /api/v1/admin/users
GET    /api/v1/admin/users/{user_id}
PATCH  /api/v1/admin/users/{user_id}       {name?, role?, status?, password?}
DELETE /api/v1/admin/users/{user_id}
```

### 3.3 Project memberships (main app)

```
GET    /api/v1/admin/projects/{pid}/members                 # any auth'd user
POST   /api/v1/admin/projects/{pid}/members                 # owner or admin
       body: {user_id, role}
PATCH  /api/v1/admin/projects/{pid}/members/{user_id}       # owner or admin
       body: {role}
DELETE /api/v1/admin/projects/{pid}/members/{user_id}       # owner or admin
```

### 3.4 Content pipeline (gateway → content_pipeline)

Every content_pipeline endpoint is reachable as
`/api/v1/cp/<original-path>`. For example:

```
POST   /api/v1/cp/projects                          # create project (admin only)
GET    /api/v1/cp/projects                          # list (filtered by membership)
GET    /api/v1/cp/projects/{pid}                    # member access
GET    /api/v1/cp/projects/{pid}/scenarios
POST   /api/v1/cp/projects/{pid}/scenarios          # create scenario
POST   /api/v1/cp/projects/{pid}/scenarios/{sid}/start-images
GET    /api/v1/cp/projects/{pid}/scenarios/{sid}/progress  # ⭐ aggregate read
POST   /api/v1/cp/projects/{pid}/weekly-plans/generate
POST   /api/v1/cp/projects/{pid}/plan-slots/{slot_id}/publish-now
GET    /api/v1/cp/projects/{pid}/cost-summary
POST   /api/v1/cp/projects/{pid}/auto-generation-rules
... (every endpoint described below in § 4)
```

The gateway forwards body, query string, and method as-is. Response is
returned verbatim. The only headers stripped are hop-by-hop (Host,
Content-Length, Authorization — replaced with the service's
X-API-Key).

### 3.5 IG scraper (gateway → ig_scraper, **admin only**)

```
GET    /api/v1/scraper/accounts            # ig accounts being managed
GET    /api/v1/scraper/jobs
GET    /api/v1/scraper/targets
POST   /api/v1/scraper/jobs                # enqueue manual scrape
... etc
```

Scraper endpoints aren't project-scoped, so non-admin users can't see
them. Use scraper UI sparingly — it's an ops surface.

---

## 4. content_pipeline endpoint groups (full list)

All of these are available at `/api/v1/cp/<path>`. The path below omits
the `/cp/` prefix; just prepend it on the panel side.

### Projects, brand kits, social accounts, templates, music

```
POST/GET/PATCH/DELETE /projects[/{pid}]
GET/POST/PATCH/DELETE /projects/{pid}/brand-kits[/{kit_id}]
GET/POST/PATCH/DELETE /projects/{pid}/social-accounts[/{acct_id}]
GET/POST/PATCH/DELETE /projects/{pid}/templates[/{tpl_id}]
GET/POST/PATCH/DELETE /projects/{pid}/music-tracks[/{track_id}]
POST                  /projects/{pid}/assets/upload-url    # presigned PUT
```

### References + intake + curator

```
POST /projects/{pid}/references/upload                # admin manual upload
POST /projects/{pid}/references/import-from-scraper   # ig_post_id
GET  /projects/{pid}/references
GET  /projects/{pid}/references/{rid}
PATCH /projects/{pid}/references/{rid}
POST /projects/{pid}/references/{rid}/archive
GET  /projects/{pid}/references/{rid}/usage-check
GET  /projects/{pid}/references/{rid}/dedup-check?max_distance=N
POST /projects/{pid}/references/{rid}/curate
GET  /projects/{pid}/inbox/candidates
POST/GET/PATCH/DELETE /projects/{pid}/intake-rules[/{rule_id}]
```

### Scenarios + scenes + variants

```
POST  /projects/{pid}/scenarios       {reference_id, target_variants[], force?, reuse_reason?, ...}
GET   /projects/{pid}/scenarios?status=&limit=&offset=
GET   /projects/{pid}/scenarios/{sid}
GET   /projects/{pid}/scenarios/{sid}/progress         # ⭐ aggregate read
PATCH /projects/{pid}/scenarios/{sid}                   # scenario_json + caption + variants edits
POST  /projects/{pid}/scenarios/{sid}/analyze
POST  /projects/{pid}/scenarios/{sid}/approve
POST  /projects/{pid}/scenarios/{sid}/regenerate
POST  /projects/{pid}/scenarios/{sid}/start-images
GET   /projects/{pid}/scenarios/{sid}/scene-renders
POST  /projects/{pid}/scenarios/{sid}/scenes/{idx}/regenerate-image  body: {aspect_ratio?, prompt_override?}
POST  /projects/{pid}/scenarios/{sid}/start-videos
POST  /projects/{pid}/scenarios/{sid}/scenes/{idx}/regenerate-video  body: {aspect_ratio?, motion_override?}
POST  /projects/{pid}/scenarios/{sid}/start-audio
POST  /projects/{pid}/scenarios/{sid}/regenerate-voiceover  body: {voice_id_override?, text_override?}
POST  /projects/{pid}/scenarios/{sid}/reselect-music   body: {music_track_id?}
POST  /projects/{pid}/scenarios/{sid}/start-compose
GET   /projects/{pid}/scenarios/{sid}/render-variants
POST  /projects/{pid}/scenarios/{sid}/render-variants/{vid}/recompose
POST  /projects/{pid}/scenarios/{sid}/render-variants/{vid}/approve
POST  /projects/{pid}/scenarios/{sid}/approve-final
GET   /projects/{pid}/scenarios/{sid}/generation-calls
```

### Posting strategy + plans + slots + stock + calendar

```
GET/PUT /projects/{pid}/posting-strategy
POST    /projects/{pid}/weekly-plans/generate      {week_start, fill}
GET     /projects/{pid}/weekly-plans
GET     /projects/{pid}/weekly-plans/{plan_id}
GET     /projects/{pid}/weekly-plans/{plan_id}/slots
POST    /projects/{pid}/weekly-plans/{plan_id}/approve
POST    /projects/{pid}/weekly-plans/{plan_id}/refill
POST/PATCH/DELETE /projects/{pid}/plan-slots[/{slot_id}]
POST    /projects/{pid}/plan-slots/{slot_id}/assign-variant
POST    /projects/{pid}/plan-slots/{slot_id}/skip
POST    /projects/{pid}/plan-slots/{slot_id}/publish-now
GET     /projects/{pid}/plan-slots/{slot_id}/publish-jobs
GET     /projects/{pid}/stock?preset=ig_reels
GET     /projects/{pid}/calendar?from=&to=
```

### Auto-generation rules

```
POST/GET/PATCH/DELETE /projects/{pid}/auto-generation-rules[/{rule_id}]
POST                  /projects/{pid}/auto-generation-rules/{rule_id}/run-now
```

### Media assets + cost

```
GET /projects/{pid}/media-assets/{asset_id}
GET /projects/{pid}/media-assets/{asset_id}/preview-url?ttl=3600   # ⭐ rendering
GET /projects/{pid}/media-assets/{asset_id}/history
GET /projects/{pid}/cost-summary?from=&to=
```

### Model routes (provider/model swap)

```
GET   /projects/{pid}/model-routes
POST  /projects/{pid}/model-routes
PATCH /projects/{pid}/model-routes/{route_id}
DELETE ...
GET   /global/model-routes               # admin-only platform defaults
POST/PATCH/DELETE /global/model-routes/{route_id}
```

---

## 5. State machines (READ THIS)

### `scenarios.status`

```
draft → analyzing → pending_review → approved
  → generating_images → images_ready
  → generating_videos → videos_ready
  → generating_audio → audio_ready
  → composing → final_pending_review → approved_final
                                        ↘ failed
```

Render the detail page as a **stepper**, each transition a button.
Failed → admin clicks `regenerate`.

### `scene_renders.status`

`pending → generating_image → image_ready → generating_video → video_ready` (or `failed`)

Render as **scenes × aspect_groups grid**, status badge per cell.

### `render_variants.status`

`pending → composing → ready → approved → published` (or `failed`)

### `plan_slots.status`

`empty → filling → ready → scheduled → publishing → published` (or
`failed` / `skipped`)

`empty` slots with `suggested_variant_ids` get a "Pick one" badge in
auto_suggest mode.

### `publish_jobs.status`

`pending → uploading → processing → published` (or `failed`)

---

## 6. Special patterns

### 6.1 Presigned upload (any asset)

API never proxies bytes. Browser uploads directly to S3.

```
1. POST /api/v1/cp/projects/{pid}/assets/upload-url
   body: {kind: "brand_logo" | "template_video" | "music" | "reference_media" | "misc",
          filename: "logo.png", content_type?: "image/png"}
   → {upload_url, s3_key, expires_in, method: "PUT", required_headers}

2. PUT <upload_url> with the bytes
   Headers: Content-Type matching what you sent in step 1

3. PATCH the relevant resource with the returned s3_key:
   PATCH /api/v1/cp/projects/{pid}/brand-kits/{kit_id}
   body: {logo_s3_key: "<s3_key from step 1>"}
```

### 6.2 Preview URL (rendering images / videos)

`media_assets.s3_key` is **NOT a URL**. Always:

```
GET /api/v1/cp/projects/{pid}/media-assets/{asset_id}/preview-url?ttl=3600
→ {asset_id, s3_key, preview_url, expires_in}
```

Use `preview_url` in `<img src=...>` / `<video src=...>`. Cache the
response for `expires_in - 60` seconds.

### 6.3 Aggregate scenario progress (recommended)

Instead of polling 4 endpoints (scenario, scene-renders,
render-variants, generation-calls):

```
GET /api/v1/cp/projects/{pid}/scenarios/{sid}/progress
→ {
    scenario, scenes[], variants[], voiceover, progress, cost
  }
```

Poll every 2s while in flight; stop when status terminal.

### 6.4 Reuse policy 409

```
POST /api/v1/cp/projects/{pid}/scenarios
body: {reference_id, target_variants: ["ig_reels"], force: false}

# If the reference was used before AND project.reuse_policy != 'silent':
→ 409
{detail: {
  error: "reference_already_used",
  previously_used: true,
  usage_count: 2,
  last_used_days_ago: 14,
  previous_scenarios: [{...}],
  project_reuse_policy: "warn",
  hint: "set force=true and supply reuse_reason to override"
}}
```

UX: modal with previous scenarios + "override with reason" form. Resend
with `force: true, reuse_reason: "..."`.

### 6.5 Polling cadence

| Resource | Interval | Stop when |
|---|---|---|
| `/scenarios/{id}/progress` (preferred) | 2s | scenario.status terminal |
| `scenario.status` | 2s | terminal status |
| `scene-renders` | 2s | every row image_ready / video_ready / failed |
| `render-variants` | 3s | every row ready / approved / failed |
| `publish-jobs` | 5s | latest published / failed |
| Cost summary | on-demand | — |
| Calendar | on-demand + after slot edits | — |

Don't poll while the tab is hidden (`document.visibilityState`).

### 6.6 Drag-drop on calendar

```
GET /api/v1/cp/projects/{pid}/calendar?from=...&to=...
PATCH /api/v1/cp/projects/{pid}/plan-slots/{slot_id}
body: {scheduled_at: "2026-05-13T16:00:00Z"}
```

Optimistic update on the client; rollback on non-2xx.

---

## 7. Suggested information architecture

```
[Project switcher in sidebar — affects /cp/projects/{pid}/* below]

📥 Inbox            → /references?status=candidate
📚 References       → /references (full library)
🎬 Scenarios        → /scenarios kanban
   └ Detail page    → state-machine stepper + scene grid + audio panel
                      + variants gallery + cost drilldown
                      (powered by /scenarios/{id}/progress)
📅 Plan             → /weekly-plans calendar + slot drawer
📦 Stock            → /stock
📊 Cost             → /cost-summary
⚙ Settings         → brand kits / social accounts / templates / music /
                      posting-strategy / intake-rules / model-routes /
                      auto-generation-rules

👥 Admin            → /admin/users (admin only) + /admin/projects/{pid}/members
🤖 Scraper          → /scraper/* (admin only — separate top-level nav)
```

---

## 8. Pitfalls

1. **Don't display `s3_key` directly.** Always go through `/preview-url`.
2. **Don't poll a hidden tab.** Use `document.visibilityState`.
3. **Versioned regenerate** is one-shot per call. `/history` shows the chain.
4. **State transitions are strict.** 409 means the API refused — surface it.
5. **`weekly-plans/generate` is idempotent.** Safe for "Refill" buttons.
6. **Reuse policy `block`** denies even with `force: true`. Read
   `project_reuse_policy` from the 409 body.
7. **Presigned URL TTL** caps at 86400s (24h). Refresh on demand.
8. **Publishing needs publicly-fetchable URLs.** In dev (MinIO +
   localhost), `publish-now` will fail with unreachable-URL errors.
   Show a "Publishing only works in prod" banner in dev.
9. **Captions / hashtags** live on `plan_slots.caption_override` and
   `scenarios.default_caption`. Slot wins; falls back to scenario.
10. **`scenario.target_variants`** is locked after `approved`. Surface a
    lock badge once the scenario advances past pending_review.
11. **Gateway returns 502** when content_pipeline / ig_scraper is down.
    Distinct from 401/403 (auth) and 5xx from the downstream service
    (those pass through). Show "Service unavailable" for 502.

---

## 9. Not exposed (don't build)

- Multi-image carousel feed posts.
- Webhook receivers from Meta / TikTok (we poll inside workers).
- Real-time WebSocket / SSE — polling only.
- Per-slot caption preview endpoint — compose in panel from
  `slot.caption_override` + `scenario.default_caption`.
- Forgot-password / email reset — admins reset via
  `PATCH /admin/users/{id}` (admin only).
- 2FA / WebAuthn / SSO.

---

## 10. Auth bootstrap

On first deploy, set in main app's `.env`:

```
BOOTSTRAP_ADMIN_EMAIL=admin@yourdomain.com
BOOTSTRAP_ADMIN_PASSWORD=<strong-password>
BOOTSTRAP_ADMIN_NAME=Admin
ADMIN_JWT_SECRET=<openssl rand -hex 32>
```

Restart main app → first admin user is created. Remove the
`BOOTSTRAP_ADMIN_*` vars from prod env after first login. The row
stays; bootstrap only fires when zero admins exist.

After login, the panel:
1. Calls `POST /admin/users` to invite teammates.
2. Calls `POST /admin/projects/{pid}/members` to grant project access.

---

## 11. Quick links

- Main app health: `GET /health` and `GET /api/v1/health`
- OpenAPI: `GET /api/v1/openapi.json` (Swagger UI at `/docs`)
- Metrics: `GET /metrics`
- This doc: `services/content_pipeline/ADMIN_API_GUIDE.md`
- Per-service status: `services/content_pipeline/CLAUDE.md`
- Backend design: `services/content_pipeline/PLAN.md`

---

## 12. Reporting drift

If an endpoint behaves unexpectedly:

1. Check the relevant service's `CLAUDE.md` for "What's deliberately
   stubbed" notes.
2. Cross-reference with this guide's § 4.
3. If the contract truly drifted, file a backend issue with the
   request/response pair.
