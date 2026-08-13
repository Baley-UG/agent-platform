"""Prometheus metrics configuration for the application.

This module sets up and configures Prometheus metrics for monitoring the application.
"""

from prometheus_client import Counter, Histogram, Gauge
from starlette.requests import Request
from starlette.routing import Match
from starlette_prometheus import metrics, PrometheusMiddleware


class SafePrometheusMiddleware(PrometheusMiddleware):
    """starlette_prometheus 0.9's `get_path_template` calls `route.path`
    on every entry of `app.routes` — FastAPI ≥0.141 wraps included
    routers in a lazy `_IncludedRouter` that matches requests but has
    no `.path`, which 500s EVERY request through the middleware.
    Override with a getattr guard; unmatched/lazy entries fall back to
    the raw URL path (metrics get a slightly less templated label for
    those routes — harmless)."""

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

# Request metrics
http_requests_total = Counter("http_requests_total", "Total number of HTTP requests", ["method", "endpoint", "status"])

http_request_duration_seconds = Histogram(
    "http_request_duration_seconds", "HTTP request duration in seconds", ["method", "endpoint"]
)

# Database metrics
db_connections = Gauge("db_connections", "Number of active database connections")

# Custom business metrics
orders_processed = Counter("orders_processed_total", "Total number of orders processed")

llm_inference_duration_seconds = Histogram(
    "llm_inference_duration_seconds",
    "Time spent processing LLM inference",
    ["model"],
    buckets=[0.1, 0.3, 0.5, 1.0, 2.0, 5.0]
)



llm_stream_duration_seconds = Histogram(
    "llm_stream_duration_seconds",
    "Time spent processing LLM stream inference",
    ["model"],
    buckets=[0.1, 0.5, 1.0, 2.0, 5.0, 10.0]
)


def setup_metrics(app):
    """Set up Prometheus metrics middleware and endpoints.

    Args:
        app: FastAPI application instance
    """
    # Add Prometheus middleware (FastAPI ≥0.141-safe subclass).
    app.add_middleware(SafePrometheusMiddleware)

    # Add metrics endpoint
    app.add_route("/metrics", metrics)
