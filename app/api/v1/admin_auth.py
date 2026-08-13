"""Admin panel auth endpoints, including Baley SSO (Authentik OIDC).

Implements the contract the admin UI expects (admin repo,
docs/backend-api.md § 3.1):

    POST /admin/auth/login            {email, password} -> tokens + user
    POST /admin/auth/refresh          {refresh_token}   -> rotated pair
    POST /admin/auth/logout           {refresh_token}   -> 204
    GET  /admin/auth/me                                 -> user
    POST /admin/auth/change-password                    -> user
    GET  /admin/auth/oidc/login       -> 302 Authentik authorize
    GET  /admin/auth/oidc/callback    -> 302 UI /auth/callback#tokens

Refresh tokens are stateless JWTs (typ=refresh). Rotation issues a new
pair on every refresh; there is no server-side revocation list yet, so
logout is client-side only (returns 204 for contract compatibility).
"""

import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from jose import JWTError, jwt

from app.api.v1.auth import get_current_user
from app.core.config import settings
from app.core.limiter import limiter
from app.core.logging import logger
from app.models.user import User
from app.schemas.admin_auth import (
    AdminChangePasswordRequest,
    AdminLoginRequest,
    AdminRefreshRequest,
    AdminTokenResponse,
    AdminUserRead,
)
from app.services.database import DatabaseService
from app.utils.auth import create_access_token
from app.utils.sanitization import sanitize_email, validate_password_strength

router = APIRouter()
db_service = DatabaseService()

ACCESS_TTL = timedelta(minutes=settings.ADMIN_ACCESS_TOKEN_TTL_MINUTES)
REFRESH_TTL = timedelta(days=settings.ADMIN_REFRESH_TOKEN_TTL_DAYS)
STATE_TTL = timedelta(minutes=10)


def _user_read(user: User) -> AdminUserRead:
    return AdminUserRead(id=user.id, email=user.email, created_at=user.created_at)


def _mint_pair(user: User) -> AdminTokenResponse:
    access = create_access_token(str(user.id), expires_delta=ACCESS_TTL)
    refresh = jwt.encode(
        {
            "sub": str(user.id),
            "typ": "refresh",
            "exp": datetime.now(UTC) + REFRESH_TTL,
            "iat": datetime.now(UTC),
            "jti": secrets.token_urlsafe(8),
        },
        settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )
    return AdminTokenResponse(
        access_token=access.access_token,
        refresh_token=refresh,
        expires_at=access.expires_at,
        user=_user_read(user),
    )


@router.post("/login", response_model=AdminTokenResponse)
@limiter.limit(settings.RATE_LIMIT_ENDPOINTS["login"][0])
async def admin_login(request: Request, body: AdminLoginRequest):
    """Email + password login with a JSON body (admin panel contract)."""
    try:
        email = sanitize_email(body.email)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid email format")
    user = await db_service.get_user_by_email(email)
    if not user or not user.verify_password(body.password.get_secret_value()):
        raise HTTPException(status_code=401, detail="Incorrect email or password")
    logger.info("admin_login", user_id=user.id)
    return _mint_pair(user)


@router.post("/refresh", response_model=AdminTokenResponse)
async def admin_refresh(body: AdminRefreshRequest):
    """Rotate a refresh token into a fresh access/refresh pair."""
    try:
        payload = jwt.decode(body.refresh_token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid refresh token")
    if payload.get("typ") != "refresh" or payload.get("sub") is None:
        raise HTTPException(status_code=401, detail="Invalid refresh token")
    user = await db_service.get_user(int(payload["sub"]))
    if user is None:
        raise HTTPException(status_code=401, detail="User not found")
    return _mint_pair(user)


@router.post("/logout", status_code=204)
async def admin_logout(body: AdminRefreshRequest):
    """Stateless logout: the client discards its tokens."""
    return Response(status_code=204)


@router.get("/me", response_model=AdminUserRead)
async def admin_me(user: User = Depends(get_current_user)):
    """Return the authenticated user in the admin panel shape."""
    return _user_read(user)


@router.post("/change-password", response_model=AdminUserRead)
async def admin_change_password(body: AdminChangePasswordRequest, user: User = Depends(get_current_user)):
    """Change the password of the authenticated user."""
    if not user.verify_password(body.current_password.get_secret_value()):
        raise HTTPException(status_code=401, detail="Current password is incorrect")
    new_password = body.new_password.get_secret_value()
    validate_password_strength(new_password)
    await db_service.update_user_password(user.id, User.hash_password(new_password))
    return _user_read(user)


# --------------------------------------------------------------------
# Baley SSO (Authentik, OIDC authorization code flow)
# --------------------------------------------------------------------


def _oidc_enabled() -> bool:
    return bool(settings.OIDC_ISSUER and settings.OIDC_CLIENT_ID and settings.OIDC_CLIENT_SECRET)


def _oidc_endpoint(name: str) -> str:
    """Authentik keeps authorize/token/userinfo global while the issuer is per-app.

    OIDC_ISSUER looks like https://<host>/application/o/<slug>/ — endpoints live
    at https://<host>/application/o/<name>/.
    """
    root = settings.OIDC_ISSUER.split("/application/o/")[0]
    return f"{root}/application/o/{name}/"


@router.get("/oidc/login")
async def oidc_login():
    """Redirect the browser to the Authentik authorize endpoint."""
    if not _oidc_enabled():
        raise HTTPException(status_code=404, detail="SSO is not configured")
    state = jwt.encode(
        {"typ": "oidc_state", "exp": datetime.now(UTC) + STATE_TTL, "jti": secrets.token_urlsafe(8)},
        settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )
    params = urlencode(
        {
            "client_id": settings.OIDC_CLIENT_ID,
            "redirect_uri": settings.OIDC_REDIRECT_URI,
            "response_type": "code",
            "scope": "openid profile email",
            "state": state,
        }
    )
    return RedirectResponse(f"{_oidc_endpoint('authorize')}?{params}")


@router.get("/oidc/callback")
async def oidc_callback(code: str, state: str):
    """Exchange the code, upsert the user, hand tokens to the admin UI."""
    if not _oidc_enabled():
        raise HTTPException(status_code=404, detail="SSO is not configured")
    try:
        payload = jwt.decode(state, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
        if payload.get("typ") != "oidc_state":
            raise JWTError("wrong state type")
    except JWTError:
        raise HTTPException(status_code=400, detail="Invalid or expired state")

    token_url = _oidc_endpoint("token")
    userinfo_url = _oidc_endpoint("userinfo")

    async with httpx.AsyncClient(timeout=15) as client:
        token_resp = await client.post(
            token_url,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": settings.OIDC_REDIRECT_URI,
                "client_id": settings.OIDC_CLIENT_ID,
                "client_secret": settings.OIDC_CLIENT_SECRET,
            },
        )
        if token_resp.status_code != 200:
            logger.error("oidc_token_exchange_failed", status=token_resp.status_code, body=token_resp.text[:200])
            raise HTTPException(status_code=502, detail="SSO token exchange failed")
        oidc_access = token_resp.json().get("access_token")

        userinfo_resp = await client.get(userinfo_url, headers={"Authorization": f"Bearer {oidc_access}"})
        if userinfo_resp.status_code != 200:
            raise HTTPException(status_code=502, detail="SSO userinfo failed")
        info = userinfo_resp.json()

    try:
        email = sanitize_email(info.get("email", ""))
    except ValueError:
        raise HTTPException(status_code=502, detail="SSO did not return a valid email")
    if not email:
        raise HTTPException(status_code=502, detail="SSO did not return an email")

    user = await db_service.get_user_by_email(email)
    if user is None:
        # SSO-only account: unguessable local password, login via Baley only.
        user = await db_service.create_user(email=email, password=User.hash_password(secrets.token_urlsafe(32)))
        logger.info("oidc_user_provisioned", email=email)

    pair = _mint_pair(user)
    fragment = urlencode(
        {
            "access_token": pair.access_token,
            "refresh_token": pair.refresh_token,
            "expires_at": pair.expires_at.isoformat(),
        }
    )
    return RedirectResponse(f"{settings.ADMIN_UI_URL.rstrip('/')}/auth/callback#{fragment}")
