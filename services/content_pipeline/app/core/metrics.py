"""Prometheus metrics.

Same pattern as ig_scraper: a `setup_metrics(app)` that mounts `/metrics`
plus middleware-level request counters. Service-specific counters are
declared at module load so they show up in Prometheus immediately.
"""

from prometheus_client import Counter, Histogram
from starlette_prometheus import PrometheusMiddleware, metrics


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
    app.add_middleware(PrometheusMiddleware)
    app.add_route("/metrics", metrics)
