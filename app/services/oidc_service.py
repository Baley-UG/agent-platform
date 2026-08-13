"""OIDC SSO against the company Authentik instance.

Authorization Code Flow + PKCE. The panel's "Sign in with SSO" button
hits `/api/v1/admin/auth/oidc/login`, we redirect to Authentik, the
user authenticates there, Authentik calls back to
`<APP_URL>/auth/callback`, we exchange the code, validate the id_token
against Authentik's JWKS, map the user by email (auto-created on first
login, no local password), and set an 8-hour HttpOnly session cookie.

Library: authlib — id_token validation (JWKS, iss/aud/exp/nonce) and
the S256 PKCE helpers come from it; the two HTTP calls (token + JWKS/
discovery) go through httpx directly for full control of timeouts.

Security decisions:
- `state` defeats CSRF on the callback; `nonce` binds the id_token to
  this browser; both live in short-lived (10 min) HttpOnly cookies
  signed implicitly by being HttpOnly + SameSite=Lax on our origin.
- PKCE (S256) protects the code even if the redirect leaks — Authentik
  supports it for confidential clients and it costs nothing.
- SSO users get NO local password: `hashed_password` is set to an
  unusable sentinel (bcrypt of 72 random bytes, discarded) so the
  password login path can never match.
- Session ceiling is `OIDC_SESSION_MAX_HOURS` (default 8) — both the
  JWT `exp` and the cookie `Max-Age`.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from datetime import UTC, datetime
from typing import Optional
from urllib.parse import urlencode

import httpx
from fastapi import HTTPException, status
from sqlmodel import Session, select

from app.core.config import settings
from app.core.logging import logger
from app.models.user import User


class OIDCError(RuntimeError):
    """Raised for any OIDC protocol failure. Callers convert to a
    login-page redirect with a generic error (never leak protocol
    detail to the browser)."""


# ---------------------------------------------------------------------------
# Discovery + JWKS (cached)
# ---------------------------------------------------------------------------

_DISCOVERY_TTL_SECONDS = 3600
_discovery_cache: dict = {}
_discovery_fetched_at: float = 0.0
_jwks_cache: dict = {}
_jwks_fetched_at: float = 0.0


def _require_enabled() -> None:
    if not settings.OIDC_ENABLED:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="SSO is not configured on this deployment",
        )


def get_discovery() -> dict:
    """Fetch (and cache) the OpenID Provider metadata."""
    global _discovery_cache, _discovery_fetched_at
    _require_enabled()
    now = time.monotonic()
    if _discovery_cache and (now - _discovery_fetched_at) < _DISCOVERY_TTL_SECONDS:
        return _discovery_cache
    url = f"{settings.OIDC_ISSUER}/.well-known/openid-configuration"
    try:
        resp = httpx.get(url, timeout=10.0)
        resp.raise_for_status()
        doc = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.error("oidc_discovery_failed", url=url, error=str(exc))
        raise OIDCError(f"discovery failed: {exc}") from exc
    for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        if key not in doc:
            raise OIDCError(f"discovery document missing {key}")
    _discovery_cache = doc
    _discovery_fetched_at = now
    return doc


def get_jwks() -> dict:
    """Fetch (and cache) the provider's signing keys."""
    global _jwks_cache, _jwks_fetched_at
    now = time.monotonic()
    if _jwks_cache and (now - _jwks_fetched_at) < _DISCOVERY_TTL_SECONDS:
        return _jwks_cache
    doc = get_discovery()
    try:
        resp = httpx.get(doc["jwks_uri"], timeout=10.0)
        resp.raise_for_status()
        jwks = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.error("oidc_jwks_failed", error=str(exc))
        raise OIDCError(f"jwks fetch failed: {exc}") from exc
    _jwks_cache = jwks
    _jwks_fetched_at = now
    return jwks


# ---------------------------------------------------------------------------
# PKCE + authorize URL
# ---------------------------------------------------------------------------


def generate_pkce_pair() -> tuple[str, str]:
    """Return `(code_verifier, code_challenge)` per RFC 7636 (S256)."""
    verifier = secrets.token_urlsafe(64)[:128]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def build_authorize_url(*, state: str, nonce: str, code_challenge: str) -> str:
    doc = get_discovery()
    params = {
        "response_type": "code",
        "client_id": settings.OIDC_CLIENT_ID,
        "redirect_uri": settings.OIDC_REDIRECT_URI,
        "scope": settings.OIDC_SCOPES,
        "state": state,
        "nonce": nonce,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{doc['authorization_endpoint']}?{urlencode(params)}"


# ---------------------------------------------------------------------------
# Token exchange + validation
# ---------------------------------------------------------------------------


async def exchange_code(*, code: str, code_verifier: str) -> dict:
    """Swap the authorization code for tokens. Returns the raw token
    response (`access_token`, `id_token`, …)."""
    doc = get_discovery()
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": settings.OIDC_REDIRECT_URI,
        "client_id": settings.OIDC_CLIENT_ID,
        "client_secret": settings.OIDC_CLIENT_SECRET,
        "code_verifier": code_verifier,
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(doc["token_endpoint"], data=data)
    except httpx.HTTPError as exc:
        raise OIDCError(f"token endpoint unreachable: {exc}") from exc
    if resp.status_code >= 400:
        logger.warning(
            "oidc_token_exchange_failed",
            status=resp.status_code,
            body=resp.text[:500],
        )
        raise OIDCError(f"token exchange failed ({resp.status_code})")
    try:
        payload = resp.json()
    except ValueError as exc:
        raise OIDCError("token endpoint returned non-JSON") from exc
    if "id_token" not in payload:
        raise OIDCError("token response missing id_token")
    return payload


def validate_id_token(id_token: str, *, nonce: str) -> dict:
    """Verify signature (JWKS), issuer, audience, expiry, and nonce.
    Returns the claims dict."""
    # Lazy import: authlib is only needed when SSO is actually used —
    # deployments without OIDC configured (and dev images built before
    # the dependency landed) must still boot cleanly.
    try:
        from authlib.jose import JsonWebToken
    except ImportError as exc:  # pragma: no cover
        raise OIDCError("authlib is not installed on this deployment") from exc

    jwks = get_jwks()
    jwt = JsonWebToken(["RS256", "ES256"])
    try:
        claims = jwt.decode(
            id_token,
            jwks,
            claims_options={
                "iss": {"essential": True, "value": settings.OIDC_ISSUER},
                "aud": {"essential": True, "value": settings.OIDC_CLIENT_ID},
                "exp": {"essential": True},
            },
        )
        claims.validate(leeway=30)
    except Exception as exc:  # noqa: BLE001 — authlib raises many subclasses
        logger.warning("oidc_id_token_invalid", error=str(exc))
        raise OIDCError(f"id_token validation failed: {exc}") from exc
    token_nonce = claims.get("nonce")
    if not token_nonce or not secrets.compare_digest(str(token_nonce), nonce):
        raise OIDCError("id_token nonce mismatch")
    return dict(claims)


# ---------------------------------------------------------------------------
# User mapping
# ---------------------------------------------------------------------------


def role_from_groups(groups: Optional[list]) -> str:
    """Map the Authentik `groups` claim to our global role.

    Membership in `OIDC_ADMIN_GROUP` → 'admin'; everything else →
    'member'. Pure function — unit-tested directly.
    """
    if isinstance(groups, list) and settings.OIDC_ADMIN_GROUP in groups:
        return "admin"
    return "member"


def find_or_create_user(session: Session, claims: dict) -> User:
    """Resolve the OIDC subject to a local user row by email.

    - Email is normalized lowercase; the unique index on `users.email`
      is the duplicate guard (idempotent across concurrent first
      logins — the loser of the race falls back to a re-read).
    - First login auto-creates the row with an UNUSABLE password so
      the password form can never authenticate an SSO account.
    - Every login re-syncs `name` and the group-derived role, so
      revoking the Authentik admin group demotes the user here on
      their next login.
    """
    email = str(claims.get("email") or "").strip().lower()
    if not email:
        raise OIDCError("id_token has no email claim (is the 'email' scope granted?)")

    role = role_from_groups(claims.get("groups"))
    name = str(claims.get("name") or claims.get("preferred_username") or "").strip() or None

    user = session.exec(select(User).where(User.email == email)).first()
    if user is None:
        user = User(
            email=email,
            # Unusable sentinel: random 64-byte secret hashed and thrown
            # away — nothing can ever verify against it.
            hashed_password=User.hash_password(secrets.token_urlsafe(48)),
            name=name,
            role=role,
            status="active",
        )
        session.add(user)
        try:
            session.commit()
        except Exception:  # noqa: BLE001 — unique-email race with a parallel first login
            session.rollback()
            user = session.exec(select(User).where(User.email == email)).first()
            if user is None:
                raise
        else:
            session.refresh(user)
        logger.info("oidc_user_created", email=email, role=role)
    if user.status != "active":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="account disabled")

    # Re-sync mutable attributes on every login.
    changed = False
    if name and user.name != name:
        user.name = name
        changed = True
    if user.role != role and user.role != "service":
        user.role = role
        changed = True
    user.last_login_at = datetime.now(UTC)
    session.add(user)
    session.commit()
    session.refresh(user)
    if changed:
        logger.info("oidc_user_synced", email=email, role=user.role)
    return user


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------


def build_end_session_url(id_token: Optional[str]) -> str:
    """RP-initiated logout URL — ends the Authentik session too.

    Falls back to the local login page when the provider doesn't
    advertise `end_session_endpoint` (shouldn't happen with Authentik).
    """
    try:
        doc = get_discovery()
    except (OIDCError, HTTPException):
        return settings.OIDC_POST_LOGOUT_REDIRECT_URI
    endpoint = doc.get("end_session_endpoint")
    if not endpoint:
        return settings.OIDC_POST_LOGOUT_REDIRECT_URI
    params = {"post_logout_redirect_uri": settings.OIDC_POST_LOGOUT_REDIRECT_URI}
    if id_token:
        params["id_token_hint"] = id_token
    return f"{endpoint}?{urlencode(params)}"
