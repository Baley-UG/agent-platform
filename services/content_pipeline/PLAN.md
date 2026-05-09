# content_pipeline — PLAN

A service that uses AI to analyze reference content (scraped from Instagram/TikTok or manually
uploaded), produces similar Instagram/TikTok content, plans it on a calendar, and publishes it.
Independent from the existing `ig_scraper` service but integrates with it.

---

## 1. Stack & Provider Decisions

| Topic | Decision | Notes |
|---|---|---|
| Framework | FastAPI (mirror existing pattern) | Same structure as `services/ig_scraper` |
| DB | Postgres (shared instance), separate schema: `content_pipeline` | Read-only access to `ig_scraper` schema |
| Job queue | RQ (Redis), shared Redis | Multiple queues, independently scalable workers |
| LLM (analyzer) | OpenRouter via central `model_routes` table | Vision-capable model required; provider/model swappable at runtime without redeploy |
| T2I (scene images) | fal.ai (Flux), routed via `model_routes` | Provider/model selectable per task |
| Video generation | Seedance (image-to-video), routed via `model_routes` | Async polling |
| TTS | ElevenLabs (default), routed via `model_routes` | `voice_id` per brand_kit |
| AI model registry | Central `model_routes` table | Maps logical task → provider + model + params; admin-editable; supports fallback chains |
| Cost ledger | `generation_calls` table | One row per external API call; tokens, units, USD cost, latency |
| Music | Self-hosted library (S3 upload) | Suno/Udio integration later |
| Compose | Self-hosted ffmpeg worker (RQ) | CPU-bound, separate container |
| Storage | Dev: MinIO (local). Prod: Hetzner S3 | Same boto3 code, only env differs |
| Publishing | Instagram Graph API (M16), TikTok Content Posting API (M17+) | Separate `social_accounts` table |
| Tenancy | **Project** is a first-class entity | X/Y projects fully isolated; everything is scoped by `project_id` |

### Provider Adapter Pattern

Each external provider gets an interface + concrete implementation:

```
app/services/providers/
  llm/        analyzer.py            (OpenRouterClient, env-driven model)
  image/      base.py, fal.py        (FluxImageProvider)
  video/      base.py, seedance.py   (later: kling.py, runway.py)
  tts/        base.py, elevenlabs.py
  social/     base.py, instagram.py, tiktok.py   (publishing)
```

Swapping a provider = touching one file. Easy to test (fakes per interface).

---

## 2. Data Model

### 2.1 `projects` (tenant)
```
id (uuid), slug (unique), name, status, created_at
posting_strategy_id, default_brand_kit_id, default_social_account_id
reuse_policy: enum('block','warn','silent') default 'warn'
weekly_budget_cap_usd numeric
```

### 2.2 `brand_kits`
```
id, project_id, name, is_default
logo_s3_url, font_family, primary_color, secondary_color
voice_id (TTS), tts_lang, tts_settings jsonb
style_prompt_suffix text   # appended to T2I prompt
created_at
```

### 2.3 `social_accounts` (publishing accounts — **distinct from `ig_accounts` in scraper**)
```
id, project_id, provider enum('instagram','tiktok')
handle, external_account_id   # IG business id / TT user id
credentials_encrypted jsonb   # access_token, refresh_token, expires_at
status: active|expired|revoked
last_used_at
```

### 2.4 `content_references` (source-agnostic reference pool)
```
id, project_id
source_provider enum('instagram','tiktok','manual_upload')
source_external_id text       # ig_post.pk / tt_video.id / null for manual
source_url text
media_s3_url text             # downloaded media copy in our S3
poster_s3_url text            # thumbnail
caption text, transcript text, hashtags text[]
metadata jsonb                # views, likes, comments, duration, etc.
content_hash bytea            # perceptual hash (dedup, M14+)
caption_embedding vector(1536) # pgvector (dedup, M14+)
imported_at, imported_by (auto|user_id)
status enum('candidate','approved','archived')

UNIQUE(project_id, source_provider, source_external_id)
```

### 2.5 `reference_intake_rules` (rule-based reference selection)
```
id, project_id, name, enabled
conditions jsonb {
  min_score, min_engagement_rate, media_types, posted_within_days,
  min_duration_sec, max_duration_sec, language, must_have_caption
}
action enum('auto_import','queue_for_review') default 'queue_for_review'
priority int
```

### 2.6 `reference_usages` (dedup tracking)
```
id, reference_id, scenario_id, project_id, created_at
status: produced|published|abandoned
```

### 2.7 `scenarios`
```
id, project_id, reference_id (nullable for manual scenarios)
status enum(
  'draft','pending_review','approved',
  'generating_images','images_ready',
  'generating_videos','videos_ready',
  'generating_audio','audio_ready',
  'composing','final_pending_review','approved_final',
  'failed'
)
scenario_json jsonb {
  duration_sec, hook, cta, scenes[], music{mood,bpm_range}, outro_template_id
}
target_variants text[]        # ['ig_reels','tiktok','ig_feed_45']
target_aspect_groups text[]   # derived: ['9:16','1:1']
generation_cost_usd numeric
created_by, created_at, updated_at
```

### 2.8 `scene_renders` (master scene outputs reused across variants)
```
id, scenario_id, scene_idx int, aspect_ratio enum('9:16','1:1','4:5','16:9')
image_asset_id, video_asset_id (FK media_assets)
status, error
```

### 2.9 `render_variants` (one row per platform/format final)
```
id, scenario_id, preset_key   # 'ig_reels','tiktok','ig_story','ig_feed_45'
status: pending|composing|ready|approved|published|failed
final_asset_id (FK media_assets)
thumbnail_asset_id
render_recipe jsonb           # ffmpeg decisions, idempotent regen
duration_sec, file_size_bytes
created_at, approved_at
```

### 2.10 `media_assets`
```
id, project_id
type enum('reference_media','scene_image','scene_video','voiceover','music','final_video','thumbnail','template_video')
s3_url, s3_key, mime_type, size_bytes
width, height, duration_sec
parent_scenario_id, parent_scene_idx (nullable)
metadata jsonb
status: ready|deleted|expired
created_at
```

### 2.11 `templates` (video parts — outro, intro, lower-third, etc.)
```
id, project_id, name
kind enum('intro','outro','lower_third','sticker','transition')
video_s3_url, duration_sec, aspect_ratio
insertion_rules jsonb { position: 'append'|'prepend', overlay_zone, audio_handling }
created_at
```

### 2.12 `music_tracks`
```
id, project_id, name
audio_s3_url, duration_sec, bpm, mood text[], tags text[]
license enum('owned','licensed','public_domain'), license_doc_url
```

### 2.13 `posting_strategy` (one per project)
```
id, project_id (unique)
timezone
weekly_quota jsonb            # {ig_reels:5, ig_story:14, ...}
preferred_slots jsonb         # {ig_reels:["Mon 19:00",...]}
min_gap_minutes jsonb
blackout jsonb
fill_strategy enum('manual','auto_suggest','auto_fill') default 'auto_suggest'
auto_generate_if_empty enum('off','suggest','auto') default 'suggest'
approval_required_before_publish bool default true
weekly_budget_cap_usd numeric
```

### 2.14 `weekly_plans`
```
id, project_id, week_start_date  # Monday
status: draft|approved|active|archived
generated_at, generated_by
notes
```

### 2.15 `plan_slots`
```
id, weekly_plan_id, project_id
scheduled_at timestamptz
social_account_id, content_type enum('post','story','reel','tiktok_video')
variant_preset                # ig_reels, tiktok, ig_story, ig_feed_45...
source_kind enum('stock','scenario','manual','empty')
variant_id (FK render_variants, nullable)
reference_id (nullable, when scenario is generating)
status enum('empty','filling','ready','scheduled','publishing','published','failed','skipped')
suggested_variant_ids uuid[]  # for auto_suggest mode
publish_job_id, last_error
```

### 2.16 `publish_jobs`
```
id, plan_slot_id, social_account_id, provider
provider_container_id, provider_media_id  # IG container, TT publish_id
status: pending|uploading|processing|published|failed
attempts, last_error, response jsonb
created_at, published_at
```

### 2.17 `auto_generation_rules` (M16)
```
id, project_id, schedule_cron
pick_strategy enum('highest_score','newest','diverse')
daily_quota int
target_variants text[]
budget_cap_usd numeric
approval_required bool default true
```

### 2.18 `model_routes` (central AI model registry)

Maps a logical task to a provider + model + params. Admin-editable through the API; changes apply
to the next call without redeploy. Multiple rows for the same task form a fallback chain (sorted
by `priority`).

```
model_routes
  id, project_id (nullable; null row = global default)
  task_key text                # 'scenario_analysis','scene_image','scene_video','voiceover_tts','music_select_llm', ...
  provider text                # 'openrouter','fal','seedance','elevenlabs','anthropic_direct','openai_direct','ollama_local'
  model_id text                # 'anthropic/claude-sonnet-4.6','fal-ai/flux-pro/v1.1', ...
  params jsonb                 # {temperature, max_tokens, quality, ...}
  priority int                 # lower = primary, higher = fallback
  enabled bool default true

  # pricing snapshot (for cost estimates)
  cost_unit text               # 'input_token','output_token','image','video_second','audio_second','call'
  cost_per_unit_usd numeric(10,8)
  pricing_updated_at timestamptz

  created_by, created_at, updated_at
  UNIQUE(project_id, task_key, priority)
```

Resolution: `model_router.resolve(project_id, task_key)` checks project-scoped rows first, then
global (`project_id IS NULL`). Returns the lowest-priority enabled row; primary failure falls
through to the next.

Admin endpoints:
```
GET    /projects/{pid}/model-routes
PUT    /projects/{pid}/model-routes/{task_key}    # body: provider, model_id, params, priority
POST   /projects/{pid}/model-routes/{task_key}/test  # latency + cost dry run
GET    /global/model-routes                       # global defaults
```

### 2.19 `generation_calls` (per-API-call cost ledger)

One row per external provider call. Written by every provider client wrapper after a successful
or failed call. Aggregations (per scenario, per project, per week) roll up from this table.

```
generation_calls
  id (uuid), project_id
  scenario_id (nullable), scene_idx (nullable), variant_id (nullable)
  task_key text                # 'scenario_analysis','scene_image','scene_video', ...
  provider text, model_id text
  request_id text              # provider-side id for trace lookups

  # usage (only relevant fields populated per provider)
  input_tokens int, output_tokens int, cached_tokens int
  image_count int, video_seconds numeric, audio_seconds numeric
  unit_count int               # generic counter for "per call" providers

  cost_usd numeric(10,6)       # computed from model_routes pricing snapshot at call time
  status enum('success','failed','timeout','rate_limited')
  latency_ms int, error text
  created_at timestamptz default now()
```

Indexes: `(project_id, created_at)`, `(scenario_id)`, `(provider, model_id, created_at)`.

Triggers / rollups:
- `scenarios.generation_cost_usd` updated by trigger on insert (or scheduled rollup).
- Weekly budget enforcement reads `SUM(cost_usd) WHERE project_id=? AND created_at >= week_start`.
- Admin dashboard endpoint `GET /projects/{pid}/cost-summary?from=...&to=...`.

### Indexes & FKs
- `content_references(project_id, status, imported_at)` index
- `plan_slots(scheduled_at, status)` partial index `WHERE status IN ('ready','scheduled')`
- `media_assets(project_id, type)` index
- `render_variants(scenario_id, status)` index
- All tables FK to `projects(id)` with cascade rules — soft delete recommended for projects.

---

## 3. Service Topology

```
services/
  ig_scraper/         (existing, untouched)
  content_pipeline/   (new)
    app/
      api/v1/
        projects.py
        brand_kits.py
        social_accounts.py
        references.py
        intake_rules.py
        scenarios.py
        scenes.py
        variants.py
        templates.py
        music.py
        plans.py
        slots.py
        posting_strategy.py
        publish.py
        assets.py            # presigned upload urls
      core/
        config.py            # env settings
        s3.py                # boto3 client (MinIO + Hetzner S3)
        security.py
      services/
        providers/
          llm/openrouter.py
          image/fal.py
          video/seedance.py
          tts/elevenlabs.py
          social/instagram.py
          social/tiktok.py
        analyzer.py          # scenario extraction
        renderer.py          # ffmpeg compose
        planner.py           # weekly plan generation
        intake.py            # rule-based reference import
      workers/
        analyzer_worker.py   # queue: 'analyzer'
        image_worker.py      # queue: 'image_gen'
        video_worker.py      # queue: 'video_gen'
        audio_worker.py      # queue: 'audio_gen'
        render_worker.py     # queue: 'media_render'  (ffmpeg)
        publish_worker.py    # queue: 'publish'
        planner_worker.py    # queue: 'planner'  (cron)
      schemas/               # pydantic
      models/                # sqlalchemy
      scheduler.py           # cron jobs (weekly plan, plan filler, publisher poller)
    alembic/
    tests/
    Dockerfile
    Dockerfile.ffmpeg        # render worker (ffmpeg installed)
    pyproject.toml
```

### Queues & Workers
| Queue | Worker | Concurrency | Notes |
|---|---|---|---|
| `analyzer` | analyzer_worker | 2-4 | LLM rate limits |
| `image_gen` | image_worker | 5-10 | fal.ai parallel |
| `video_gen` | video_worker | 3-5 | Seedance expensive, async polling |
| `audio_gen` | audio_worker | 2-3 | ElevenLabs rate limits |
| `media_render` | render_worker (ffmpeg) | CPU count | Separate container, CPU-bound |
| `publish` | publish_worker | 1-2 | Per-account rate limits |
| `planner` | planner_worker | 1 | Cron-driven |

### docker-compose Additions
```yaml
content_pipeline_api:        # FastAPI
content_pipeline_worker:     # general worker (analyzer, image, video, audio, publish, planner)
content_pipeline_render:     # ffmpeg-installed image
content_pipeline_scheduler:  # rq-scheduler / cron
```

`content_pipeline_render` Dockerfile: `FROM python:3.12-slim` + `apt-get install ffmpeg`.

### Dev Storage: MinIO (docker-compose.override.yml)

For local development we run MinIO — S3-compatible. Same boto3 code talks to Hetzner S3 in prod
by changing env only:

```yaml
minio:
  image: minio/minio
  command: server /data --console-address ":9001"
  environment:
    MINIO_ROOT_USER: minioadmin
    MINIO_ROOT_PASSWORD: minioadmin
  ports:
    - "9000:9000"    # S3 API
    - "9001:9001"    # Web console (file browser)
  volumes:
    - minio_data:/data

minio_init:                # auto-create bucket
  image: minio/mc
  depends_on: [minio]
  entrypoint: >
    sh -c "
    mc alias set local http://minio:9000 minioadmin minioadmin &&
    mc mb -p local/content-pipeline-dev || true
    "
```

**Env (dev)**:
```
S3_ENDPOINT=http://minio:9000
S3_BUCKET=content-pipeline-dev
S3_ACCESS_KEY=minioadmin
S3_SECRET_KEY=minioadmin
S3_REGION=us-east-1
S3_USE_PATH_STYLE=true     # required for MinIO
```

**Env (prod, Hetzner)**:
```
S3_ENDPOINT=https://<region>.your-objectstorage.com
S3_BUCKET=...
S3_ACCESS_KEY=...
S3_SECRET_KEY=...
S3_USE_PATH_STYLE=false
```

The MinIO web console at `http://localhost:9001` is a handy file browser during development.

---

## 4. End-to-End Flow

```
[scrape]  ig_scraper → ig_posts
            ↓ (intake_rules + admin approval)
[reference]  content_references
            ↓ (scenario create)
[analyze]  LLM → scenario_json (scenes, transitions, voiceover, mood)
            ↓ admin edit & approve
[image_gen]  fal.ai Flux → one image per scene per aspect group
            ↓ admin gate (regen optional)
[video_gen]  Seedance I2V → one 5-10s clip per scene
            ↓ admin gate (optional)
[audio_gen]  ElevenLabs TTS + music_library selection → voiceover.mp3 + music.mp3
            ↓
[compose]  ffmpeg worker → one compose per render_variant:
  - concat scene videos + transitions
  - append outro template
  - on-screen text overlay (respect safe zones)
  - voiceover + ducked music mix
  - loudness normalize, encode to platform spec
            ↓ admin final review
[approve]  render_variant.status = approved → stock pool or plan slot
            ↓
[plan]  attached to weekly_plan slot (manual / auto_suggest / auto_fill)
            ↓ at scheduled_at
[publish]  IG Graph API / TikTok Content Posting → published
```

### 4.1 Multi-Aspect (multiple output formats)

Presets in `scenario.target_variants` fan out by **aspect group**:
- `9:16` group: ig_reels, ig_story, tiktok, ig_shorts → single master scene generation, compose differs per platform
- `1:1` / `4:5` group: ig_feed → separate image_gen + (optional) video_gen

The `scene_renders` table (scenario_id, scene_idx, aspect_ratio) avoids re-rendering master scene
files when generating multiple variants in the same aspect group.

`presets.py` (code, not env):
```python
PRESETS = {
  "ig_reels":   {aspect:"9:16", w:1080, h:1920, fps:30, max_duration:90, audio_lufs:-14, container:"mp4"},
  "tiktok":     {aspect:"9:16", w:1080, h:1920, fps:30, max_duration:600, audio_lufs:-14, safe_zones:{top:130,bottom:280}},
  "ig_story":   {aspect:"9:16", w:1080, h:1920, max_duration:60, safe_zones:{top:250,bottom:250}},
  "ig_feed_45": {aspect:"4:5",  w:1080, h:1350, max_duration:60},
  "ig_feed_11": {aspect:"1:1",  w:1080, h:1080},
}
```

### 4.2 Regeneration at Every Level (admin override)

The admin can regenerate any artifact in the pipeline. Each regeneration creates a **new version**;
the previous version stays in S3 (governed by retention policy) and can be rolled back.

| Level | Endpoint | Cost | Notes |
|---|---|---|---|
| Whole scenario JSON | `POST /scenarios/{id}/regenerate` | LLM call | Re-runs analyzer; previous scenario_json archived |
| Single scene image | `POST /scenarios/{id}/scenes/{idx}/regenerate-image` | 1× T2I | Optional `prompt_override` body |
| Single scene video | `POST /scenarios/{id}/scenes/{idx}/regenerate-video` | 1× I2V | Reuses approved image |
| Voiceover | `POST /scenarios/{id}/regenerate-voiceover` | 1× TTS | Optional `text_override`, `voice_id_override` |
| Music selection | `POST /scenarios/{id}/reselect-music` | 0 | Library swap only |
| Compose only | `POST /variants/{id}/recompose` | ffmpeg time only | Scene videos unchanged; only ffmpeg recipe re-runs |
| Full variant | `POST /variants/{id}/regenerate-all` | full | Everything from scenes onward |

**Versioning**: `media_assets` carries `version int` and `replaced_by_id uuid`. The currently
active asset is the latest version with `replaced_by_id IS NULL`. Rollback = a swap back, not a delete.

**UI surface**: each artifact's response includes `version`, `previous_version_id`, and a
`history_url` listing prior versions for diff/preview.

### 4.3 Reference Reuse Control

`POST /scenarios` body:
```json
{ "reference_id": "...", "force": false, "reuse_reason": "..." }
```

If `force=false` and the reference was already used → **409 Conflict** with payload:
```json
{
  "error": "reference_already_used",
  "previously_used": true,
  "usage_count": 2,
  "previous_scenarios": [{"id":"...","status":"published","published_at":"..."}],
  "last_used_days_ago": 14,
  "project_reuse_policy": "warn"
}
```

`projects.reuse_policy="block"` rejects even with `force=true`.

---

## 5. Weekly Planning

### Skeleton + Filling (default `fill_strategy=auto_suggest`)

`POST /projects/{pid}/weekly-plans/generate { week_start, fill: true }`:

1. Build empty slots from `posting_strategy.preferred_slots` × `weekly_quota` (`status=empty`).
2. Apply `blackout` and `min_gap` constraints.
3. **Fill strategy**:
   - **manual**: no auto fill, admin populates by hand.
   - **auto_suggest** (default): for each slot, write 2–3 stock candidates into
     `suggested_variant_ids`; admin clicks one to assign.
   - **auto_fill**: assign best stock variant automatically (FIFO or score-ranked).
4. If stock runs out → `auto_generate_if_empty` policy kicks in:
   - **off**: leave empty.
   - **suggest**: surface "generate from this reference?" suggestion to admin.
   - **auto**: enqueue scenario generation automatically (within budget cap).

### Scheduler Jobs

| Cron | Job |
|---|---|
| Sunday 18:00 | Generate next week's `weekly_plan` per project (when enabled) |
| Hourly | Retry filling `status=empty` slots (new stock or new references may be available) |
| Every minute | `scheduled_at <= now() AND status=ready` → enqueue `publish` |
| Hourly | Alert on stale `generating_*` scenarios (>2h, stuck) |

### Stock Definition

`stock_pool` is a view, not a table:
```sql
SELECT * FROM render_variants
WHERE status='approved'
  AND id NOT IN (
    SELECT variant_id FROM plan_slots
    WHERE variant_id IS NOT NULL AND status NOT IN ('failed','skipped')
  );
```

---

## 6. API Surface (summary)

```
# Project
GET/POST/PUT/DELETE /projects
GET/PUT /projects/{pid}/posting-strategy
GET/POST /projects/{pid}/brand-kits
GET/POST/PUT /projects/{pid}/social-accounts

# References
POST /projects/{pid}/references/import-from-scraper   # by ig_posts.id
POST /projects/{pid}/references/upload                # presigned URL + manual upload
GET  /projects/{pid}/references                       # filters: status, source_provider
PUT  /projects/{pid}/references/{id}/approve|archive
GET  /projects/{pid}/references/{id}/usage-check

POST /projects/{pid}/intake-rules
GET  /projects/{pid}/inbox/candidates                 # rule action=queue_for_review

# Scenarios
POST /projects/{pid}/scenarios                        # body: reference_id, target_variants, force
GET  /projects/{pid}/scenarios/{id}
PUT  /projects/{pid}/scenarios/{id}                   # admin scenario edit
POST /projects/{pid}/scenarios/{id}/approve          # → image_gen queue
POST /projects/{pid}/scenarios/{id}/scenes/{idx}/regenerate-image
POST /projects/{pid}/scenarios/{id}/animate           # images approved → video_gen
POST /projects/{pid}/scenarios/{id}/scenes/{idx}/regenerate-video
POST /projects/{pid}/scenarios/{id}/render            # variants compose

# Variants
GET  /projects/{pid}/variants/{id}
POST /projects/{pid}/variants/{id}/approve

# Templates / Music / Brand Assets
POST /projects/{pid}/templates                        # presigned upload + metadata
POST /projects/{pid}/music-tracks
POST /projects/{pid}/assets/upload-url                # generic presigned

# Plans
POST /projects/{pid}/weekly-plans/generate
GET  /projects/{pid}/weekly-plans?week=...
GET  /projects/{pid}/weekly-plans/{id}
POST /projects/{pid}/weekly-plans/{id}/approve
POST /projects/{pid}/weekly-plans/{id}/refill
GET  /projects/{pid}/calendar?from=...&to=...

POST /plan-slots
PUT  /plan-slots/{id}                                 # drag & drop
POST /plan-slots/{id}/assign-variant
POST /plan-slots/{id}/generate-now                    # generate scenario for this slot
POST /plan-slots/{id}/publish-now
POST /plan-slots/{id}/skip
DELETE /plan-slots/{id}

# Stock
GET /projects/{pid}/stock                             # variant view

# Publishing
GET /projects/{pid}/publish-jobs/{id}
```

OpenAPI is auto-generated by FastAPI; the admin panel consumes it directly.

---

## 7. ig_scraper Integration

**Cross-DB read-only access** (same Postgres, separate schema):

`POST /references/import-from-scraper { ig_post_id }`:
1. Read row from `ig_scraper.ig_posts`.
2. Copy media to the pipeline's S3 prefix (own copy, decoupled from scraper retention).
3. Insert `content_references` row (`source_provider='instagram'`, `source_external_id=pk`).

**Notification (M14+)**: when scraper passes its M8 enrichment gate, publish a Redis pub/sub
event; content_pipeline subscriber runs `intake_rules` automatically.

`ig_scraper` itself **stays unchanged** — content_pipeline talks to it as an external client.

---

## 8. Cost Control

- Every external API call writes a `generation_calls` row with tokens/units, USD cost, latency.
- Pricing snapshot lives on `model_routes.cost_per_unit_usd` — providers compute call cost using
  the route they were resolved through (immune to provider-side price changes mid-pipeline).
- Estimated cost at scenario create = scene count × per-task pricing from `model_routes`.
- `posting_strategy.weekly_budget_cap_usd` enforced via `SUM(generation_calls.cost_usd)` for the
  project's current week. Exceeding it pauses scenario generation and fires an alert webhook.
- Tier flag: `scenario.quality_tier='draft'|'final'` — draft uses cheaper rows from `model_routes`
  (we keep `priority` slot for tier overrides via params).
- Admin dashboard: per-project cost summary, per-scenario breakdown, per-provider monthly burn.

## 8.1 Cost Optimization Path (deferred)

Phase 1 uses **OpenRouter** for LLM access. OpenRouter takes a small markup (~5% with BYOK,
near-zero on standard usage). We accept this as a development-velocity premium — one API,
many models, automatic fallback.

When monthly LLM spend exceeds an internal threshold **and** the product is stable:

1. Implement `app/services/providers/llm/anthropic_direct.py` (and/or `openai_direct.py`),
   conforming to the same `LLMProvider` interface as `openrouter.py`.
2. Update the `model_routes` row for `task_key='scenario_analysis'`: set
   `provider='anthropic_direct'`, point `model_id` at the same Claude model. No schema change.
3. Direct providers unlock features OpenRouter routes around (e.g. Anthropic prompt caching for
   90% read discount on stable prompts).

Local model self-hosting (Ollama / vLLM) is a third option with its own GPU and ops cost; only
worth it past a much higher spend threshold. The provider interface accommodates it without
schema changes.

The `model_routes.provider` column is `text`, not an enum — adding a new provider is purely a
code change with no migration.

---

## 9. Security & Copyright

- Source content is **never copied verbatim** — only the abstracted scenario JSON flows into
  the generators. Original frames/audio are not fed to any provider.
- Music: scraped audio is never published; only `music_tracks` (uploaded, licensed) is used.
- IG/TT TOS: only official Graph / Content Posting APIs; no unofficial automation.
- `social_accounts.credentials_encrypted` → encrypted at rest (KMS or Fernet).
- Every endpoint enforces `project_id` scope (multi-tenant isolation).

---

## 10. Milestones

> Numbered **CP-Mx** to avoid collision with `ig_scraper`'s own M-numbering.
> Each service tracks its own milestones in its own CLAUDE.md.

### CP-M1 — Skeleton, Data Model, Storage, Project CRUD
**Goal**: new service boots; projects, brand_kits, social_accounts, asset upload all work.

- [ ] `services/content_pipeline/` skeleton (FastAPI + RQ + Alembic)
- [ ] `core/config.py`, `core/s3.py` (boto3 client; MinIO and Hetzner S3 share one code path)
- [ ] `core/security.py` (Fernet for `social_accounts.credentials_encrypted`)
- [ ] `docker-compose.override.yml`: MinIO + bucket init service
- [ ] Alembic initial migration: `projects`, `brand_kits`, `social_accounts`, `templates`,
      `music_tracks`, `media_assets` (with `version`, `replaced_by_id`), `content_references`,
      `model_routes`, `generation_calls`
- [ ] Project CRUD API
- [ ] Brand kit CRUD + asset upload (presigned URL)
- [ ] Social account CRUD (IG/TT credentials skeleton, encrypted at rest)
- [ ] Template / music upload API
- [ ] `model_routes` CRUD (admin) + seed file with global defaults per task_key
- [ ] `app/services/model_router.py` skeleton (resolution logic, used by CP-M2 onward)
- [ ] `app/services/providers/llm/base.py` interface stub (concrete impls land in CP-M2)
- [ ] docker-compose: api + worker + render + scheduler (skeletal)
- [ ] Smoke test: create project → upload brand asset → see it in S3 → resolve a model_route

### CP-M2 — References + Intake + Analyzer
- [ ] `content_references` API (import-from-scraper, manual upload)
- [ ] `reference_intake_rules` + integration with scraper M8 score
- [ ] `inbox/candidates` endpoint
- [ ] OpenRouter LLM provider implementing `LLMProvider` interface
- [ ] `analyzer_worker`: produce scenario_json + S3 keyframes; writes `generation_calls` row
- [ ] `scenarios` API: create, get, edit, approve
- [ ] `POST /scenarios/{id}/regenerate` (versioned)
- [ ] Reuse check + reuse_policy enforcement

### CP-M3 — Image Generation + Multi-Aspect
- [ ] fal.ai provider (Flux) implementing `ImageProvider` interface
- [ ] `image_worker`: parallel image generation per scene; writes `generation_calls`
- [ ] `scene_renders` aspect group fan-out (9:16, 1:1, 4:5)
- [ ] Scene image regenerate endpoint (versioned)
- [ ] Admin gate: images_ready → animate

### CP-M4 — Video Generation
- [ ] Seedance provider (I2V, async polling/webhook) implementing `VideoProvider` interface
- [ ] `video_worker`: I2V per scene; writes `generation_calls`
- [ ] Scene video regenerate (versioned)
- [ ] Admin gate

### CP-M5 — Audio + Compose
- [ ] ElevenLabs TTS provider implementing `TTSProvider` interface
- [ ] `audio_worker`: voiceover generation; writes `generation_calls`
- [ ] Music selection (library query)
- [ ] ffmpeg `render_worker`:
  - scene concat + transitions (xfade)
  - outro template insertion
  - on-screen text overlay (safe zones)
  - voiceover + music mix + ducking
  - loudness normalize + platform-spec encode
- [ ] `render_variants` per-preset compose
- [ ] Voiceover/music regenerate endpoints, variant recompose, full regenerate-all
- [ ] Final review + approve API

### CP-M6 — Posting Strategy + Weekly Plan + IG Publish
- [ ] `posting_strategy` API
- [ ] `weekly_plans` + `plan_slots` API
- [ ] Skeleton generation (preferred_slots × quota)
- [ ] auto_suggest / auto_fill / manual modes
- [ ] Drag-drop slot edit
- [ ] Stock view
- [ ] IG Graph API publisher (resumable upload + container/publish)
- [ ] Scheduler cron jobs (weekly auto-gen, plan filler, publisher poller)

### CP-M7 — TikTok + Auto-Generation
- [ ] TikTok Content Posting API publisher
- [ ] TikTok scraper support (separate `tt_videos` table in ig_scraper — its own milestone really)
- [ ] `auto_generation_rules` + scheduler
- [ ] Budget enforcement reading `generation_calls`

### CP-M8 — Quality / Enhancement
- [ ] Perceptual hash + embedding dedup (pgvector)
- [ ] AI curator (LLM filter on top of intake rules)
- [ ] Learn `preferred_slots` from IG Insights
- [ ] Suno/Udio music generation integration
- [ ] WebSocket progress events for the admin panel
- [ ] Outpaint-based cross-aspect conversion (9:16 → 16:9)

---

## 11. Known Limitations & Future Solutions

These are real production constraints surfaced during planning. None are blocking
for CP-M1..CP-M5 (which target the simple "single layer, fully generated" path),
but each has a concrete plan slot for when we hit it in a real campaign.

### 11.1 Scene granularity is semantic, not time-uniform

**Concern**: a 1.5s grid is too coarse for fast-cut hooks (0.3-0.8s flash cuts)
and too fine for narrative beats that hold for 5-8s. A reference may also pivot
locations sharply (home → mall) — that's a scene cut, not a single scene with a
motion prompt that pretends to traverse them.

**Status**: the analyzer already produces variable-length scenes (1.5-10s) and
makes the cut decisions itself. The system prompt instructs it to choose scene
boundaries by narrative beat, not by clock.

**Future polish (CP-M3 or CP-M4)**:
- Add a `scene.kind: 'master' | 'flash_cut' | 'beat_hold'` enum so the renderer
  picks an appropriate transition style and disables I2V motion on flash cuts
  (they look odd with parallax).
- Allow sub-scenes / shot lists inside one scene when a single semantic beat
  needs multiple shots (e.g. "morning routine" = pour coffee + open laptop + sit
  down, all conceptually one scene). Schema: `scene.shots[]` with shared mood
  + voiceover but distinct image/motion prompts.
- Auto-detect when a reference's transcript implies hard location pivots and
  enforce a cut, rather than letting the LLM blend them into one motion prompt.

### 11.2 Multi-region compositions (split / PiP / overlay)

**Concern**: many viral formats split the 9:16 frame:
- Top/bottom split: gameplay on top, reaction on bottom; product demo on top,
  caption text on bottom.
- Picture-in-picture: presenter overlay on full-screen background.
- Side-by-side compare: before/after, two product variants.

A single `image_prompt` per scene assumes a single full-frame composition —
inadequate for these.

**Future schema (CP-M5+)**:
```jsonc
{
  "scenes": [{
    "idx": 0,
    "duration": 6,
    "composition": "split_vertical",      // 'single' | 'split_vertical' | 'split_horizontal' | 'pip' | 'three_panel'
    "regions": [
      {"slot": "top",    "image_prompt": "...", "motion_prompt": "...", "audio": "muted"},
      {"slot": "bottom", "image_prompt": "...", "motion_prompt": "...", "audio": "primary"}
    ],
    // single-region scenes keep using the existing flat fields for backwards-compat
  }]
}
```

The compose stage adds a filter-complex layout per `composition` value (already
straightforward in ffmpeg via `vstack` / `hstack` / `overlay`). Each region is
a separate image_gen + video_gen job, so the existing fan-out logic extends
cleanly.

**When to build**: when an admin requests their first split-format project.
Until then, single-region is enough and the schema stays simple.

### 11.3 Non-generatable assets (UI screenshots, real product shots, custom logos)

**Concern**: AI image models can't reliably reproduce:
- Specific mobile app UI screenshots (the actual app being promoted)
- Real product photography (a specific bottle, a specific label)
- Brand-exact logo placements with legible micro-text
- Charts / data visualizations with real numbers

These need to be admin-supplied assets injected into the pipeline rather than
generated.

**Future schema (CP-M3 or CP-M4)**:
- Reuse `media_assets` with new `type` values: `'ui_screenshot'`, `'product_photo'`,
  `'data_viz'`, `'admin_supplied_clip'`.
- Add a `scene_asset_slots` table or a `scene.asset_injections[]` array on the
  scenario_json:
  ```jsonc
  {
    "idx": 3,
    "kind": "asset_injected",            // skip image_gen + video_gen entirely
    "duration": 2.5,
    "asset_ref": {
      "media_asset_id": "...",
      "treatment": "phone_mockup"        // 'fullscreen' | 'phone_mockup' | 'inline_overlay' | 'ken_burns'
    },
    "voiceover": "Tap install."
  }
  ```
- For `treatment='phone_mockup'`: the renderer composites the screenshot inside
  a stock phone frame template (which itself lives in `templates`), with optional
  hand/finger overlay templates.
- For `kind='mixed'`: image_gen still produces a background, but compose overlays
  the admin asset on top using `asset_injections[].overlay_box` coordinates
  (relative to the safe-zone-aware frame).

**Admin UX hooks (separate panel work)**:
- Drag-and-drop a screenshot onto a scene in the timeline → creates the
  `media_assets` row and patches the scenario_json with an `asset_injections`
  entry pointing at it.
- "Phone mockup library": uploadable phone-frame PNG/MP4 templates with a
  predefined inner-screen rectangle.

**When to build**: as soon as the first project promotes an actual app —
that's the canonical use case where this is unavoidable.

### 11.4 Other deferred items (already noted elsewhere in this file)

- Perceptual hash + caption embedding dedup across references — § 10 / CP-M8.
- AI curator (LLM filter on top of intake rules) — CP-M8.
- Outpaint-based 9:16 → 16:9 conversion — CP-M8.
- Suno / Udio music generation — CP-M8.

---

## 12. Open Notes / Future Decisions

- **Webhook vs polling** for Seedance: first version polls (simpler); webhook revisited in M14.
- **`cost_estimates` updates**: first version is manual (env/seed); later, sync from provider invoice APIs.
- **Multi-region S3**: single region (Hetzner); CDN later.
- **Backup / retention**: media_assets soft delete + S3 lifecycle policy (cold storage after 90 days).
- **Test strategy**: providers are mocked via fake adapters; integration tests use Localstack S3 + Redis.
