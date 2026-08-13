"""Prometheus metrics configuration for the application.

This module sets up and configures Prometheus metrics for monitoring the application.
"""

from prometheus_client import Counter, Histogram, Gauge
from starlette.requests import Request
from starlette_prometheus import metrics, PrometheusMiddleware


class SafePrometheusMiddleware(PrometheusMiddleware):
    """starlette_prometheus assumes every matched route has a `.path`;
    FastAPI's lazy router entries (`_IncludedRouter`) don't, which 500s
    every included route. Fall back to the raw URL path for those."""

    def get_path_template(self, request: Request):
        from starlette.routing import Match

        for route in request.app.routes:
            match, _ = route.matches(request.scope)
            if match == Match.FULL:
                return getattr(route, "path", request.url.path), True
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
    # Add Prometheus middleware
    app.add_middleware(SafePrometheusMiddleware)

    # Add metrics endpoint
    app.add_route("/metrics", metrics)
