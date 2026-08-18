"""Prometheus metrics for the ad_scraper service.

Counters are declared here so any module can import them without
re-defining. They are populated by the worker, the YouCloud client and
the mirror service as the milestones land.
"""

from fastapi import FastAPI
from prometheus_client import Counter, Histogram
from starlette.requests import Request
from starlette.routing import Match
from starlette_prometheus import PrometheusMiddleware, metrics


class SafePrometheusMiddleware(PrometheusMiddleware):
    """Same guard as ig_scraper's `app/core/metrics.py`.

    starlette_prometheus 0.9's `get_path_template` calls `route.path` on
    every entry of `app.routes`; FastAPI >=0.141 wraps included routers in
    a lazy `_IncludedRouter` that matches requests but has no `.path`,
    which 500s EVERY request through the middleware.
    """

    def get_path_template(self, request: Request):
        """Resolve the matched route's path template.

        Skips route types that don't expose one instead of raising.
        """
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
ad_jobs_total = Counter(
    "ad_jobs_total",
    "Number of ingestion jobs that reached a terminal state.",
    labelnames=("status",),
)
ad_job_duration_seconds = Histogram(
    "ad_job_duration_seconds",
    "Wall-clock time per ingestion job.",
    buckets=(1, 5, 15, 30, 60, 120, 300, 600, 1500, 3000),
)

# ---------- Content counters ----------
ad_materials_saved_total = Counter(
    "ad_materials_saved_total",
    "Materials upserted into ad_materials.",
    labelnames=("outcome",),  # new | updated
)
ad_advertisers_saved_total = Counter(
    "ad_advertisers_saved_total",
    "Advertiser entities upserted into ad_advertisers.",
)
ad_pages_fetched_total = Counter(
    "ad_pages_fetched_total",
    "materialList pages successfully fetched.",
)

# ---------- API / auth failure counters ----------
# `code` is the YouCloud `errors[0].extensions.c` value (e.g. 05:400001),
# or a synthetic label for transport-level problems.
ad_api_errors_total = Counter(
    "ad_api_errors_total",
    "YouCloud GraphQL errors, keyed by their extensions.c code.",
    labelnames=("code",),
)
ad_login_failures_total = Counter(
    "ad_login_failures_total",
    "Failed attempts to obtain a fresh YouCloud session.",
    labelnames=("reason",),
)

# ---------- Mirror counters ----------
ad_mirror_bytes_total = Counter(
    "ad_mirror_bytes_total",
    "Bytes copied from the YouCloud CDN into our S3 bucket.",
    labelnames=("kind",),  # media | poster
)
ad_mirror_failures_total = Counter(
    "ad_mirror_failures_total",
    "Mirror attempts that did not produce an S3 object.",
    labelnames=("reason",),
)

# ---------- Truncation signal ----------
# Incremented whenever a job's filter set reports more rows than the
# 10 000-row page ceiling can return. A non-zero rate means operators are
# running filters too broad to ever be fully ingested.
ad_filter_truncated_total = Counter(
    "ad_filter_truncated_total",
    "Jobs whose filter set exceeded the API's 10 000-row pagination ceiling.",
)


def setup_metrics(app: FastAPI) -> None:
    """Mount Prometheus middleware and the /metrics endpoint."""
    app.add_middleware(SafePrometheusMiddleware)
    app.add_route("/metrics", metrics)
