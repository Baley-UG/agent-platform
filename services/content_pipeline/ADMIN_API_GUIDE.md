# Admin Panel API Guide — moved

The full guide now lives in the admin panel repo:

> `/Users/batinduz/htdocs/baley/agent-platform-admin/docs/backend-api.md`

That repo is the consumer of this contract; keeping the guide there
puts it next to the code that uses it.

---

## Backend updates → sync the guide

When this service ships a new milestone (or any contract-affecting
change), update the admin-panel copy:

```bash
# from the agent-platform repo root
cp services/content_pipeline/ADMIN_API_GUIDE.md \
   ../../htdocs/baley/agent-platform-admin/docs/backend-api.md
```

…and reword the header in the destination to mark "synced from
backend at <commit>" so admin devs know the source-of-truth point.

(If you'd rather, automate it as a Makefile target.)

---

## Why this file still exists

A short pointer here means anyone reading the backend service alone
finds the contract doc. Don't put the full body here too — drift between
two copies always wins, and the admin repo is the better home.

If you want to read the guide right now, open the file at the path
above. Section index there:

1. Architecture (gateway diagram, single base URL)
2. Conventions (auth, scoping, pagination, status codes)
3. Endpoint catalog (`/admin/*`, `/cp/*`, `/scraper/*`)
4. content_pipeline endpoint groups (full list under `/cp/`)
5. State machines (scenarios, scene_renders, render_variants, plan_slots, publish_jobs)
6. Special patterns (presigned upload, preview URL, aggregate progress, reuse 409, polling cadence)
7. Suggested information architecture
8. Pitfalls (10 items)
9. Not exposed (don't build)
10. Auth bootstrap
11. Quick links
12. Reporting drift
