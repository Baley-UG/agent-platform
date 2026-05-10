"""JWT + password hashing helpers.

Access tokens: stateless JWT (HS256), 1h default. Carry `{sub, role, exp, iat, type}`.
Refresh tokens: random 32-byte base64url, hashed (SHA-256) into auth_sessions.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, InvalidHash

from app.core.config import settings


_PLACEHOLDER_JWT = "changeme-jwt-secret"

_hasher = PasswordHasher()


# ---------- passwords ----------


def hash_password(plain: str) -> str:
    if not plain or len(plain) < 8:
        raise ValueError("password must be at least 8 characters")
    return _hasher.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    if not plain or not hashed:
        return False
    try:
        _hasher.verify(hashed, plain)
        return True
    except (VerifyMismatchError, InvalidHash, Exception):  # noqa: BLE001
        return False


def needs_rehash(hashed: str) -> bool:
    try:
        return _hasher.check_needs_rehash(hashed)
    except Exception:  # noqa: BLE001
        return False


# ---------- JWT ----------


class TokenError(RuntimeError):
    """Raised on signature mismatch / expiry / malformed token."""


def _check_secret() -> None:
    if settings.CP_JWT_SECRET == _PLACEHOLDER_JWT:
        raise RuntimeError(
            "CP_JWT_SECRET is set to the placeholder value. "
            "Generate one with: openssl rand -hex 32"
        )


def issue_access_token(user_id: uuid.UUID, role: str) -> tuple[str, datetime]:
    """Returns (jwt_string, expires_at)."""
    _check_secret()
    now = datetime.now(timezone.utc)
    exp = now + timedelta(minutes=settings.CP_ACCESS_TOKEN_TTL_MINUTES)
    payload = {
        "sub": str(user_id),
        "role": role,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
        "type": "access",
    }
    token = jwt.encode(payload, settings.CP_JWT_SECRET, algorithm=settings.CP_JWT_ALGORITHM)
    return token, exp


def decode_access_token(token: str) -> dict:
    """Verify + decode. Raises TokenError on any failure."""
    _check_secret()
    try:
        payload = jwt.decode(token, settings.CP_JWT_SECRET, algorithms=[settings.CP_JWT_ALGORITHM])
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("access token expired") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenError(f"invalid access token: {exc}") from exc
    if payload.get("type") != "access":
        raise TokenError("token is not an access token")
    return payload


# ---------- refresh tokens ----------


def issue_refresh_token() -> tuple[str, str, datetime]:
    """Generate a fresh refresh token. Returns (raw_token, hash, expires_at).

    Caller persists `hash` in auth_sessions and returns `raw_token` to the
    client. We never store the raw token — its only ground truth is what
    the client holds.
    """
    raw = secrets.token_urlsafe(32)
    return raw, hash_refresh_token(raw), datetime.now(timezone.utc) + timedelta(
        days=settings.CP_REFRESH_TOKEN_TTL_DAYS
    )


def hash_refresh_token(raw: str) -> str:
    """SHA-256 hex digest. Constant cost; fine for refresh tokens (random 32 bytes)."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
