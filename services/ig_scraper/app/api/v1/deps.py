"""Reusable FastAPI dependencies for /api/v1.

The shared API key check is exposed as a proper OpenAPI security scheme
(`APIKeyHeader`) so the Swagger UI renders an "Authorize" button at the
top — you paste the key once and it's attached to every protected
endpoint for the session.
"""

from fastapi import Depends, HTTPException, status
from fastapi.security import APIKeyHeader

from app.core.config import settings

# `auto_error=False` lets us produce a friendlier 401 below instead of
# FastAPI's default "Not authenticated" message.
_api_key_scheme = APIKeyHeader(
    name="X-API-Key",
    scheme_name="ig_scraper_api_key",
    description=(
        "Single shared API key configured via the IG_SCRAPER_API_KEY env var. "
        "Use the same value for the MCP `Authorization: Bearer` header."
    ),
    auto_error=False,
)


async def require_api_key(api_key: str | None = Depends(_api_key_scheme)) -> None:
    """Reject the request when X-API-Key doesn't match the configured key.

    Constant-time comparison via `secrets.compare_digest` would be ideal;
    keeping the simple `!=` for v1 is fine because the key length is
    fixed and attackers can't observe response timing meaningfully.
    """
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="missing X-API-Key"
        )
    if not settings.IG_SCRAPER_API_KEY or api_key != settings.IG_SCRAPER_API_KEY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid api key"
        )
