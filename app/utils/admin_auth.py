"""Admin-panel JWT helpers — separate from the chat-thread JWT in `auth.py`.

- Access token: stateless JWT (HS256), 1h default. Carries
  `{sub: <user_id>, role, exp, iat, type: 'admin_access'}`.
- Refresh token: random 32-byte urlsafe string, hashed (SHA-256) into
  the `admin_session` table.

We use `python-jose` for parity with the existing chat auth.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Optional, Tuple

from jose import JWTError, jwt

from app.core.config import settings
from app.core.logging import logger


class AdminTokenError(RuntimeError):
    """Raised on signature mismatch / expiry / malformed token."""


def _check_secret() -> None:
    if not settings.ADMIN_JWT_SECRET:
        raise RuntimeError("ADMIN_JWT_SECRET (or JWT_SECRET_KEY fallback) is empty")


def issue_access_token(
    user_id: int, role: str, ttl_minutes: int | None = None
) -> Tuple[str, datetime]:
    """Returns (jwt_string, expires_at).

    `ttl_minutes` overrides the configured TTL — the OIDC SSO flow
    issues 8-hour cookie sessions (`OIDC_SESSION_MAX_HOURS`) instead of
    the short Bearer-token TTL used by the password + refresh flow.
    """
    _check_secret()
    now = datetime.now(UTC)
    exp = now + timedelta(minutes=ttl_minutes or settings.ADMIN_ACCESS_TOKEN_TTL_MINUTES)
    payload = {
        "sub": str(user_id),
        "role": role,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
        "type": "admin_access",
    }
    token = jwt.encode(payload, settings.ADMIN_JWT_SECRET, algorithm=settings.JWT_ALGORITHM)
    return token, exp


def decode_access_token(token: str) -> dict:
    _check_secret()
    try:
        payload = jwt.decode(token, settings.ADMIN_JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    except JWTError as exc:
        raise AdminTokenError(f"invalid admin access token: {exc}") from exc
    if payload.get("type") != "admin_access":
        raise AdminTokenError("token is not an admin access token")
    return payload


def issue_refresh_token() -> Tuple[str, str, datetime]:
    """Generate a fresh refresh token. Returns (raw, hash, expires_at)."""
    raw = secrets.token_urlsafe(32)
    return (
        raw,
        hash_refresh_token(raw),
        datetime.now(UTC) + timedelta(days=settings.ADMIN_REFRESH_TOKEN_TTL_DAYS),
    )


def hash_refresh_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
