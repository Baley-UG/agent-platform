"""Prometheus metrics for the ig_scraper service.

The actual scrape/job counters are populated in their respective modules
as the milestones land. M1 only registers the metric objects so other
modules can `from app.core.metrics import ig_jobs_total` etc. without
re-defining them.
"""

from fastapi import FastAPI
from prometheus_client import Counter, Histogram
from starlette.requests import Request
from starlette.routing import Match
from starlette_prometheus import PrometheusMiddleware, metrics


class SafePrometheusMiddleware(PrometheusMiddleware):
    """Same guard as the main app's `app/core/metrics.py`.

    starlette_prometheus 0.9's `get_path_template` calls `route.path` on
    every entry of `app.routes`; FastAPI >=0.141 wraps included routers in
    a lazy `_IncludedRouter` that matches requests but has no `.path`,
    which 500s EVERY request through the middleware.
    """

    def get_path_template(self, request: Request):
        for route in request.app.routes:
            try:
                match, _child = route.matches(request.scope)
            except Exception:  # noqa: BLE001 — defensive: any exotic route type
                continue
            if match == Match.FULL:
                path = getattr(route, "path", None)
                return (path or request.url.path), path is not None
        return request.url.path, False


# ---------- Job metrics ----------
ig_jobs_total = Counter(
    "ig_jobs_total",
    "Number of scrape jobs that reached a terminal state.",
    labelnames=("type", "status"),
)
ig_job_duration_seconds = Histogram(
    "ig_job_duration_seconds",
    "Wall-clock time per scrape job.",
    labelnames=("type",),
    buckets=(1, 5, 15, 30, 60, 120, 300, 600, 1500, 3000),
)

# ---------- Content counters ----------
ig_posts_saved_total = Counter(
    "ig_posts_saved_total",
    "Posts upserted into ig_posts.",
    labelnames=("job_type",),
)
ig_comments_saved_total = Counter(
    "ig_comments_saved_total",
    "Comments upserted into ig_comments.",
)
ig_stories_saved_total = Counter(
    "ig_stories_saved_total",
    "Stories inserted into ig_stories.",
)

# ---------- Failure / health counters ----------
ig_account_failures_total = Counter(
    "ig_account_failures_total",
    "Account-level failures, broken down by reason (challenge, login, parse_error, ...).",
    labelnames=("reason",),
)
ig_proxy_failures_total = Counter(
    "ig_proxy_failures_total",
    "Proxy-level failures (timeout, banned, refused, ...).",
    labelnames=("reason",),
)


def setup_metrics(app: FastAPI) -> None:
    """Mount Prometheus middleware and the /metrics endpoint."""
    app.add_middleware(SafePrometheusMiddleware)
    app.add_route("/metrics", metrics)
