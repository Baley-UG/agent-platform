"""Admin-panel auth endpoints — login, refresh, logout, me, change-password.

Mounted at `/api/v1/admin/auth`. The admin panel renders the login form;
this API only verifies email + password and issues tokens.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlmodel import Session

from app.api.v1.admin_deps import AdminPrincipal, get_db, require_admin_token
from app.schemas.admin import (
    AdminUserRead,
    ChangePasswordRequest,
    LoginRequest,
    ProjectMembershipRead,
    RefreshRequest,
    TokenResponse,
)
from app.services import admin_auth_service as svc

router = APIRouter(prefix="/admin/auth", tags=["admin-auth"])


def _user_payload(session: Session, user) -> AdminUserRead:
    memberships = svc.memberships_for_user(session, user.id)
    return AdminUserRead(
        id=user.id,
        email=user.email,
        name=user.name,
        role=user.role,
        status=user.status,
        last_login_at=user.last_login_at,
        created_at=user.created_at,
        memberships=[ProjectMembershipRead.model_validate(m) for m in memberships],
    )


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, request: Request, session: Session = Depends(get_db)) -> TokenResponse:
    user, access_token, refresh_token, exp = svc.login(
        session,
        email=payload.email,
        password=payload.password,
        user_agent=request.headers.get("user-agent"),
        ip=request.client.host if request.client else None,
    )
    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=exp,
        user=_user_payload(session, user),
    )


@router.post("/refresh", response_model=TokenResponse)
def refresh(payload: RefreshRequest, request: Request, session: Session = Depends(get_db)) -> TokenResponse:
    user, access_token, refresh_token, exp = svc.refresh(
        session,
        raw_refresh=payload.refresh_token,
        user_agent=request.headers.get("user-agent"),
        ip=request.client.host if request.client else None,
    )
    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=exp,
        user=_user_payload(session, user),
    )


@router.post("/logout", status_code=204)
def logout(payload: RefreshRequest, session: Session = Depends(get_db)) -> None:
    svc.logout(session, raw_refresh=payload.refresh_token)


@router.get("/me", response_model=AdminUserRead)
def me(
    principal: AdminPrincipal = Depends(require_admin_token), session: Session = Depends(get_db)
) -> AdminUserRead:
    return _user_payload(session, principal.user)


@router.post("/change-password", response_model=AdminUserRead)
def change_password(
    payload: ChangePasswordRequest,
    principal: AdminPrincipal = Depends(require_admin_token),
    session: Session = Depends(get_db),
) -> AdminUserRead:
    user = svc.change_password(session, principal.user, payload.current_password, payload.new_password)
    return _user_payload(session, user)
