"""Prometheus metrics.

Same pattern as ig_scraper: a `setup_metrics(app)` that mounts `/metrics`
plus middleware-level request counters. Service-specific counters are
declared at module load so they show up in Prometheus immediately.
"""

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


cp_jobs_total = Counter(
    "cp_jobs_total",
    "RQ jobs enqueued by the content_pipeline service.",
    ["queue", "task"],
)

cp_generation_calls_total = Counter(
    "cp_generation_calls_total",
    "External provider calls made by the content_pipeline (analyzer, image, video, tts, ...).",
    ["task_key", "provider", "status"],
)

cp_generation_call_latency_seconds = Histogram(
    "cp_generation_call_latency_seconds",
    "Latency of external provider calls.",
    ["task_key", "provider"],
)

cp_assets_uploaded_total = Counter(
    "cp_assets_uploaded_total",
    "Assets uploaded to S3 by kind.",
    ["kind"],
)


def setup_metrics(app) -> None:
    """Mount Prometheus middleware + /metrics endpoint."""
    app.add_middleware(SafePrometheusMiddleware)
    app.add_route("/metrics", metrics)
