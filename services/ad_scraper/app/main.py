"""FastAPI entry point for the ad_scraper API process.

The worker is a separate entry point (`python -m app.worker`) and does not
load this module. There is no scheduler process — ingestion is
operator-driven through `POST /api/v1/jobs`.
"""

from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.v1.api import api_router
from app.api.v1.health import health, ready
from app.core.config import settings
from app.core.logging import logger
from app.core.metrics import setup_metrics


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown hooks."""
    logger.info(
        "ad_scraper_api_startup",
        version=settings.VERSION,
        environment=settings.ENVIRONMENT.value,
        api_prefix=settings.API_V1_STR,
        mirror_policy=settings.AD_MIRROR_MEDIA,
        max_rows_per_filter_set=settings.max_rows_per_filter_set,
    )
    # The mounted MCP sub-app's own lifespan never runs (Starlette mounts
    # don't propagate it), so its streamable-HTTP session manager must be
    # started here or every /mcp request 500s with "Task group is not
    # initialized".
    from app.mcp_server import mcp_server  # noqa: E402 — lazy, optional

    if mcp_server is not None:
        async with mcp_server.session_manager.run():
            yield
    else:
        yield
    logger.info("ad_scraper_api_shutdown")


app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    description=settings.DESCRIPTION,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    lifespan=lifespan,
)

setup_metrics(app)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Render validation errors in a stable shape."""
    logger.error("validation_error", path=request.url.path, errors=str(exc.errors()))
    formatted = [
        {
            "field": " -> ".join(str(part) for part in err["loc"] if part != "body"),
            "message": err["msg"],
        }
        for err in exc.errors()
    ]
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": "Validation error", "errors": formatted},
    )


# Mount /api/v1/...
app.include_router(api_router, prefix=settings.API_V1_STR)

# Top-level /health and /ready are convenient for Docker probes that don't
# want to remember the API prefix.
app.add_api_route("/health", health, methods=["GET"], tags=["health"])
app.add_api_route("/ready", ready, methods=["GET"], tags=["health"])

# Mount the MCP server at /mcp (Streamable HTTP transport), mirroring
# ig_scraper. When the `mcp` package isn't installed `mcp_server` is
# None and mounting is skipped — the REST API is unaffected.


class _McpAuth:
    """Bearer/X-API-Key gate on the MCP mount.

    The REST surface has its own dependency-based key check; a Starlette
    mount bypasses FastAPI dependencies, so without this the MCP tools
    would be open to anyone who can reach the port — unacceptable the
    moment the service is exposed through a reverse proxy for remote
    Claude access. Same key as REST (`AD_SCRAPER_API_KEY`), sent as
    `Authorization: Bearer <key>` (MCP clients) or `X-API-Key`.
    """

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
            expected = settings.AD_SCRAPER_API_KEY
            ok = (
                headers.get("authorization") == f"Bearer {expected}"
                or headers.get("x-api-key") == expected
            )
            if not ok:
                from starlette.responses import JSONResponse

                await JSONResponse({"detail": "invalid or missing API key"}, status_code=401)(
                    scope, receive, send
                )
                return
        await self.inner(scope, receive, send)


try:
    from app.mcp_server import mcp_server  # noqa: E402

    if mcp_server is not None:
        try:
            app.mount("/mcp", _McpAuth(mcp_server.streamable_http_app()))
            logger.info("mcp_server_mounted", path="/mcp")
        except Exception as exc:  # noqa: BLE001
            logger.warning("mcp_mount_failed", error=str(exc))
except Exception as exc:  # noqa: BLE001
    logger.warning("mcp_import_failed", error=str(exc))


@app.get("/")
async def root():
    """Service banner."""
    return {
        "name": settings.PROJECT_NAME,
        "version": settings.VERSION,
        "environment": settings.ENVIRONMENT.value,
        "swagger_url": "/docs",
        "api_prefix": settings.API_V1_STR,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
