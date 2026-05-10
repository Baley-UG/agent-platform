"""Pure-logic tests for core/auth helpers — JWT issue/decode + password hashing."""

from __future__ import annotations

import os
import time
import uuid
from datetime import datetime, timezone

import pytest

# conftest.py already sets a real Fernet key + test env. Set CP_JWT_SECRET too.
os.environ.setdefault("CP_JWT_SECRET", "test-jwt-secret-not-the-placeholder")

from app.core import auth  # noqa: E402


def test_hash_password_round_trip():
    h = auth.hash_password("correct horse battery staple")
    assert auth.verify_password("correct horse battery staple", h) is True


def test_verify_password_rejects_wrong():
    h = auth.hash_password("password123")
    assert auth.verify_password("nope", h) is False


def test_hash_password_rejects_short_input():
    with pytest.raises(ValueError, match="8 characters"):
        auth.hash_password("short")


def test_issue_and_decode_access_token():
    user_id = uuid.uuid4()
    token, exp = auth.issue_access_token(user_id, "admin")
    payload = auth.decode_access_token(token)
    assert payload["sub"] == str(user_id)
    assert payload["role"] == "admin"
    assert payload["type"] == "access"
    assert exp > datetime.now(timezone.utc)


def test_decode_rejects_garbage():
    with pytest.raises(auth.TokenError):
        auth.decode_access_token("not.a.jwt")


def test_decode_rejects_wrong_signature():
    user_id = uuid.uuid4()
    token, _ = auth.issue_access_token(user_id, "admin")
    # Mutate the last char of the token's signature segment.
    parts = token.split(".")
    sig = parts[2]
    parts[2] = "A" + sig[1:] if sig[0] != "A" else "B" + sig[1:]
    with pytest.raises(auth.TokenError):
        auth.decode_access_token(".".join(parts))


def test_decode_rejects_refresh_token_passed_as_access():
    """The decoder hard-checks `type=access`."""
    raw, hashed, exp = auth.issue_refresh_token()
    # Build a fake JWT of type='refresh' to confirm the gate
    import jwt

    from app.core.config import settings

    payload = {
        "sub": str(uuid.uuid4()),
        "role": "admin",
        "iat": int(time.time()),
        "exp": int(time.time()) + 60,
        "type": "refresh",
    }
    token = jwt.encode(payload, settings.CP_JWT_SECRET, algorithm=settings.CP_JWT_ALGORITHM)
    with pytest.raises(auth.TokenError, match="not an access token"):
        auth.decode_access_token(token)


def test_refresh_token_hash_is_deterministic_and_unique():
    raw1, hash1, _ = auth.issue_refresh_token()
    raw2, hash2, _ = auth.issue_refresh_token()
    assert raw1 != raw2
    assert hash1 != hash2
    # Re-hashing the same raw token produces the same hash.
    assert auth.hash_refresh_token(raw1) == hash1
