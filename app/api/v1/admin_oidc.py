"""OIDC SSO endpoints — Authentik Authorization Code Flow + PKCE.

Three routes:

  GET /api/v1/admin/auth/oidc/login
      Kick off the flow. Generates state + nonce + PKCE, parks them in
      short-lived HttpOnly cookies, 302s to Authentik's authorize URL.

  GET /auth/callback                      (root-level, matches OIDC_REDIRECT_URI)
      Authentik redirects here with ?code&state. Validates state,
      exchanges the code (PKCE), validates the id_token, maps the user
      by email (auto-create on first login), sets the 8h HttpOnly
      session cookie, then 302s to the admin panel.

  GET /api/v1/admin/auth/oidc/logout
      Clears our cookies and 302s to Authentik's end_session endpoint
      (id_token_hint included) so BOTH sessions die.

Cookie inventory (all HttpOnly):
  baley_admin_session   the admin JWT — 8h, SameSite=Lax
  baley_oidc_id         raw id_token, kept only for the logout hint — 8h
  oidc_state / oidc_nonce / oidc_verifier
                        flow-transient, 10 min, cleared on callback
"""

from __future__ import annotations

import secrets
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlmodel import Session

from app.api.v1.admin_deps import get_db
from app.core.config import settings
from app.core.logging import logger
from app.services import oidc_service as oidc
from app.utils import admin_auth as core

router = APIRouter(tags=["admin-auth-oidc"])

_SESSION_COOKIE = "baley_admin_session"
_ID_TOKEN_COOKIE = "baley_oidc_id"
_FLOW_COOKIE_TTL = 600  # 10 minutes to complete the Authentik round-trip


def _flow_cookie_kwargs() -> dict:
    return {
        "httponly": True,
        "secure": settings.OIDC_COOKIE_SECURE,
        "samesite": "lax",
        "max_age": _FLOW_COOKIE_TTL,
        "path": "/",
    }


def _session_cookie_kwargs() -> dict:
    return {
        "httponly": True,
        "secure": settings.OIDC_COOKIE_SECURE,
        "samesite": "lax",
        "max_age": settings.OIDC_SESSION_MAX_HOURS * 3600,
        "path": "/",
    }


def _login_error_redirect(reason: str) -> RedirectResponse:
    """Send the browser back to the panel login with a GENERIC error.

    Protocol details go to the server log only — an attacker probing
    the callback shouldn't learn which validation step tripped.
    """
    logger.warning("oidc_login_failed", reason=reason)
    q = urlencode({"error": "sso_failed"})
    resp = RedirectResponse(url=f"{settings.ADMIN_PANEL_URL}/auth/login?{q}", status_code=302)
    for name in ("oidc_state", "oidc_nonce", "oidc_verifier"):
        resp.delete_cookie(name, path="/")
    return resp


@router.get("/admin/auth/oidc/status")
def oidc_status() -> dict:
    """Public probe for the panel's login page — decides whether to
    render the "Sign in with SSO" button. Exposes NOTHING sensitive
    (no issuer, no client id)."""
    return {"enabled": settings.OIDC_ENABLED}


@router.get("/admin/auth/oidc/login")
def oidc_login() -> RedirectResponse:
    """Start the Authorization Code Flow."""
    if not settings.OIDC_ENABLED:
        return JSONResponse(status_code=404, content={"detail": "SSO not configured"})

    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier, challenge = oidc.generate_pkce_pair()

    try:
        authorize_url = oidc.build_authorize_url(
            state=state, nonce=nonce, code_challenge=challenge
        )
    except oidc.OIDCError as exc:
        return _login_error_redirect(f"authorize url: {exc}")

    resp = RedirectResponse(url=authorize_url, status_code=302)
    kw = _flow_cookie_kwargs()
    resp.set_cookie("oidc_state", state, **kw)
    resp.set_cookie("oidc_nonce", nonce, **kw)
    resp.set_cookie("oidc_verifier", verifier, **kw)
    return resp


# NOTE: the callback is NOT on this router. OIDC_REDIRECT_URI is a
# root-level path (`<APP_URL>/auth/callback`) outside /api/v1, so
# `app/main.py` mounts `oidc_callback` directly with
# `app.get("/auth/callback")`.
async def oidc_callback(
    request: Request,
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
    session: Session = Depends(get_db),
) -> RedirectResponse:
    """Authentik redirects here. Validate everything, mint the session."""
    if not settings.OIDC_ENABLED:
        return JSONResponse(status_code=404, content={"detail": "SSO not configured"})

    if error:
        # User denied consent / provider-side failure.
        return _login_error_redirect(f"provider error: {error}")
    if not code or not state:
        return _login_error_redirect("missing code/state")

    cookie_state = request.cookies.get("oidc_state")
    cookie_nonce = request.cookies.get("oidc_nonce")
    cookie_verifier = request.cookies.get("oidc_verifier")
    if not cookie_state or not cookie_nonce or not cookie_verifier:
        return _login_error_redirect("flow cookies missing/expired")
    if not secrets.compare_digest(cookie_state, state):
        return _login_error_redirect("state mismatch")

    try:
        tokens = await oidc.exchange_code(code=code, code_verifier=cookie_verifier)
        claims = oidc.validate_id_token(tokens["id_token"], nonce=cookie_nonce)
        user = oidc.find_or_create_user(session, claims)
    except oidc.OIDCError as exc:
        return _login_error_redirect(str(exc))

    ttl_minutes = settings.OIDC_SESSION_MAX_HOURS * 60
    access_token, _exp = core.issue_access_token(user.id, user.role, ttl_minutes=ttl_minutes)

    # Land on the panel's SSO bridge page (NOT /dashboard directly):
    # the panel middleware gates on its own origin cookie, which only
    # the bridge can set after probing /me with our HttpOnly session.
    resp = RedirectResponse(
        url=f"{settings.ADMIN_PANEL_URL}/auth/sso-complete", status_code=302
    )
    kw = _session_cookie_kwargs()
    resp.set_cookie(_SESSION_COOKIE, access_token, **kw)
    # id_token retained ONLY as the logout hint — HttpOnly like the rest.
    resp.set_cookie(_ID_TOKEN_COOKIE, tokens["id_token"], **kw)
    for name in ("oidc_state", "oidc_nonce", "oidc_verifier"):
        resp.delete_cookie(name, path="/")
    logger.info("oidc_login_ok", user_id=user.id, email=user.email, role=user.role)
    return resp


@router.get("/admin/auth/oidc/logout")
def oidc_logout(request: Request) -> RedirectResponse:
    """Kill both sessions: ours (cookies) and Authentik's (end_session)."""
    id_token = request.cookies.get(_ID_TOKEN_COOKIE)
    end_session_url = oidc.build_end_session_url(id_token)
    resp = RedirectResponse(url=end_session_url, status_code=302)
    resp.delete_cookie(_SESSION_COOKIE, path="/")
    resp.delete_cookie(_ID_TOKEN_COOKIE, path="/")
    return resp
