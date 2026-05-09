"""Health and readiness endpoints."""

from datetime import datetime, timezone

from fastapi import status
from fastapi.responses import JSONResponse

from app.core.config import settings
from app.services.database import health_check


async def health() -> JSONResponse:
    """Liveness check — always returns 200 if the process can serve a request."""
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "status": "ok",
            "service": settings.PROJECT_NAME,
            "version": settings.VERSION,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )


async def ready() -> JSONResponse:
    """Readiness check — verifies the DB is reachable."""
    if health_check():
        return JSONResponse(status_code=status.HTTP_200_OK, content={"status": "ready"})
    return JSONResponse(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, content={"status": "not_ready"})
