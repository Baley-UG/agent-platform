# ig_scraper — Operations Runbook

This is the on-call playbook for the Instagram competitor scraper.
Pair it with `services/ig_scraper/CLAUDE.md` (architectural overview)
and `docs/instagram-scraper-plan.md` (full design).

## Service topology

Three Docker services from one image:

| Service | Command | What it does |
| - | - | - |
| `ig-scraper-api` | `uvicorn app.main:app --port 8081` | REST + MCP control plane |
| `ig-scraper-worker` | `python -m app.worker` | Claims jobs, runs scrapes |
| `ig-scraper-scheduler` | `python -m app.scheduler` | Tick + daily + webhook + canary loops |

Each process writes a heartbeat row to `ig_worker_heartbeat`. `/ready`
reports degraded if any heartbeat is older than `IG_HEARTBEAT_STALE_AFTER_SECONDS`.

## Day-to-day operations

### Add a scraping account

```bash
curl -X POST http://ig-scraper-api:8081/api/v1/accounts \
  -H "X-API-Key: $IG_SCRAPER_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"username": "...", "password": "...", "proxy_id": "<uuid>", "timezone": "Europe/Istanbul"}'
```

Then run the login flow:

```bash
curl -X POST http://ig-scraper-api:8081/api/v1/accounts/<id>/login \
  -H "X-API-Key: $IG_SCRAPER_API_KEY" \
  -d '{}'
```

If the response says `status: "challenge_required"`, fetch the SMS / email
code and retry the same endpoint with `{"verification_code": "123456"}`.

### Add a proxy

```bash
curl -X POST http://ig-scraper-api:8081/api/v1/proxies \
  -H "X-API-Key: $IG_SCRAPER_API_KEY" \
  -d '{"protocol": "http", "host": "...", "port": 1234, "username": "...", "password": "...", "label": "brightdata-resi-tr"}'

curl -X POST http://ig-scraper-api:8081/api/v1/proxies/<id>/test \
  -H "X-API-Key: $IG_SCRAPER_API_KEY"
```

### Track a username or hashtag

```bash
curl -X POST http://ig-scraper-api:8081/api/v1/targets \
  -H "X-API-Key: $IG_SCRAPER_API_KEY" \
  -d '{"kind": "user", "value": "competitorbrand", "interval_hours": 24}'
```

The first scheduler tick will enqueue `user_feed_full` (+ `user_stories`,
+ `user_highlights` if enabled). Subsequent runs use `user_feed_incremental`.

### Approve a discovered target

Hashtag scans surface promising authors as `pending_review`:

```bash
curl http://ig-scraper-api:8081/api/v1/targets?status=pending_review \
  -H "X-API-Key: $IG_SCRAPER_API_KEY"

curl -X POST http://ig-scraper-api:8081/api/v1/targets/<id>/activate \
  -H "X-API-Key: $IG_SCRAPER_API_KEY"
```

## Common failures

### `status: "challenge_required"` on an account

Instagram is asking for a verification code. The account is paused
(no jobs run on it) until the operator re-runs the login flow with
`verification_code`. If challenges keep coming back, the account is
either flagged or its proxy is residentially "burned" — rotate the
proxy and try again.

### Proxy `failure_count` climbing

`POST /proxies/<id>/test` is the canary; expect `latency_ms` < 4s on a
working residential proxy. If failure_count crosses 3 the pool puts it
in `cooldown`. Replace the proxy if it stays dead for 24h.

### `instagrapi` parse errors / canary failing

When the canary target's job suddenly starts failing with parse errors
or `KeyError` on instagrapi calls, IG has changed their private API
shape. Steps:

1. Pin the current version in `pyproject.toml`.
2. Bump `instagrapi` and re-test against the canary (`POST /targets/<canary_id>/run-now`).
3. If the new version works, deploy. If not, file an upstream issue and
   pause non-canary scrapes until upstream lands a fix.

The canary alert exists so we catch this before the rest of the fleet
churns through challenge_required loops.

### Job stuck in `running`

Either the worker process died mid-scrape or a slow IG call is in
flight. The scheduler doesn't reclaim — operator action required:

```sql
UPDATE ig_scrape_jobs
SET status = 'queued', started_at = NULL
WHERE id = '<job_id>' AND status = 'running' AND started_at < now() - interval '1 hour';
```

### Webhook deliveries stuck

Check `ig_webhook_deliveries` for status='pending' / 'failed':

```sql
SELECT id, webhook_id, event_type, status, attempt, error
FROM ig_webhook_deliveries
WHERE status IN ('pending', 'failed')
ORDER BY scheduled_for DESC
LIMIT 20;
```

If a webhook URL is dead, mark its parent paused:

```sql
UPDATE ig_webhooks SET status = 'paused' WHERE id = '<id>';
```

## Key rotation

### `IG_SCRAPER_API_KEY`

Single shared secret for REST + MCP. Generate a new one with
`openssl rand -hex 32`, update `.env`, restart all three services
(api, worker, scheduler) plus every caller. No DB migration needed.

### `IG_SECRET_KEY` (Fernet)

This protects every account password and proxy password at rest.
Rotation is non-trivial:

1. Generate a new key.
2. Pause the worker (`docker compose stop ig-scraper-worker`).
3. Run a one-shot script that decrypts every `password_enc` /
   `proxy.password_enc` with the OLD key and re-encrypts with the NEW
   key. (Not committed — keep it in your runbook repo, never in this
   service.)
4. Update `.env`, restart services.

If you rotate the key without re-encrypting, every login + proxy test
will start failing with the `InvalidToken` error from
`app/services/crypto.py`. That error message includes the rotation
hint.

## Monitoring (Grafana)

Dashboard JSON: `grafana/dashboards/ig_scraper.json`. Mount it via the
existing Grafana provisioning at the repo root.

Key panels:
- **Posts/Stories saved (24h)** — should never drop to zero unless the
  fleet is genuinely paused.
- **Active accounts / Tracked targets** — both should be steady.
- **Score distribution** — heavy left-skew means the daily fleet is
  mostly catching low-quality content; consider raising
  `IG_MIN_SCORE_FOR_ENRICH` or pausing low-signal hashtags.
- **Account challenges (24h)** — non-zero = throttling problem; widen
  delays or shrink quotas.
- **Webhook deliveries failing** — if non-zero, check the
  `ig_webhook_deliveries.error` column.
- **Top scoring posts** — sanity check that scoring still ranks
  meaningfully.

## Phase 2 prerequisites (M11–M13)

Already in place from Phase 1:
- Postgres `pgvector` extension (the main app's `db` image is
  `pgvector/pgvector:pg16`).
- `raw` JSONB columns hold the full instagrapi payload, so M12 LLM
  feature extraction won't need a re-scrape.
- MCP tool surface ready to grow with `find_similar_posts` (M11) and
  `get_author_style` (M13).

Missing until M11 lands:
- `CREATE EXTENSION pgvector` migration.
- An embedding API key in `.env` (default OpenAI `text-embedding-3-small`).
