"""Reusable FastAPI dependencies for /api/v1.

The shared API key check is exposed as a proper OpenAPI security scheme
(`APIKeyHeader`) so the Swagger UI renders an "Authorize" button — you
paste the key once and it's attached to every protected endpoint.
"""

import secrets
from typing import Iterator

from fastapi import Depends, HTTPException, status
from fastapi.security import APIKeyHeader
from sqlmodel import Session

from app.core.config import settings
from app.services.database import engine, read_engine

# `auto_error=False` lets us produce a friendlier 401 below instead of
# FastAPI's default "Not authenticated" message.
_api_key_scheme = APIKeyHeader(
    name="X-API-Key",
    scheme_name="ad_scraper_api_key",
    description="Single shared API key configured via the AD_SCRAPER_API_KEY env var.",
    auto_error=False,
)


async def require_api_key(api_key: str | None = Depends(_api_key_scheme)) -> None:
    """Reject the request when X-API-Key doesn't match the configured key."""
    if not api_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing X-API-Key")
    expected = settings.AD_SCRAPER_API_KEY
    if not expected or not secrets.compare_digest(api_key, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid api key")


def get_session() -> Iterator[Session]:
    """Write session for endpoints that mutate rows."""
    session = Session(engine, expire_on_commit=False)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_read_session() -> Iterator[Session]:
    """Read-only session, routed to the replica when one is configured."""
    session = Session(read_engine, expire_on_commit=False)
    try:
        yield session
    finally:
        session.close()
