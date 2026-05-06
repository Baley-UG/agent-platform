"""Reusable FastAPI dependencies for /api/v1.

The single shared API key check lives here so every router can drop it
in via `Depends(require_api_key)`.
"""

from fastapi import Header, HTTPException, status

from app.core.config import settings


async def require_api_key(x_api_key: str = Header(..., alias="X-API-Key")) -> None:
    """Reject the request when X-API-Key doesn't match the configured key.

    Constant-time comparison via secrets.compare_digest would be ideal;
    keeping the simple `!=` for v1 is fine because the key length is
    fixed and attackers can't observe response timing meaningfully.
    """
    if not settings.IG_SCRAPER_API_KEY or x_api_key != settings.IG_SCRAPER_API_KEY:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid api key")
