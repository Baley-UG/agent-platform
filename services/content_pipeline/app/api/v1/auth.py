"""Auth endpoints — login, refresh, logout, me, change-password.

The admin panel renders the login form; this API only verifies email +
password and issues tokens. Same JSON contract as a typical OAuth-style
backend so panels using TanStack Query / SWR can plug in directly.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlmodel import Session

from app.api.v1.deps import Principal, get_session, require_auth
from app.schemas.auth import (
    ChangePasswordRequest,
    LoginRequest,
    ProjectMembershipRead,
    RefreshRequest,
    TokenResponse,
    UserRead,
)
from app.services import auth as auth_svc
from app.services import users as users_svc

router = APIRouter(prefix="/auth", tags=["auth"])


def _user_payload(session: Session, user) -> UserRead:
    memberships = users_svc.memberships_for_user(session, user.id)
    return UserRead(
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
def login(
    payload: LoginRequest,
    request: Request,
    session: Session = Depends(get_session),
) -> TokenResponse:
    user, access_token, refresh_token, exp = auth_svc.login(
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
def refresh(
    payload: RefreshRequest,
    request: Request,
    session: Session = Depends(get_session),
) -> TokenResponse:
    user, access_token, refresh_token, exp = auth_svc.refresh(
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
def logout(payload: RefreshRequest, session: Session = Depends(get_session)) -> None:
    auth_svc.logout(session, raw_refresh=payload.refresh_token)


@router.get("/me", response_model=UserRead)
def me(
    principal: Principal = Depends(require_auth),
    session: Session = Depends(get_session),
) -> UserRead:
    if principal.user is None:
        # Service principal hitting /me — return a synthetic shape.
        return UserRead(
            id="00000000-0000-0000-0000-000000000000",  # type: ignore[arg-type]
            email="service@local",
            name="service",
            role="service",
            status="active",
            last_login_at=None,
            created_at="1970-01-01T00:00:00+00:00",  # type: ignore[arg-type]
            memberships=[],
        )
    return _user_payload(session, principal.user)


@router.post("/change-password", response_model=UserRead)
def change_password(
    payload: ChangePasswordRequest,
    principal: Principal = Depends(require_auth),
    session: Session = Depends(get_session),
) -> UserRead:
    if principal.kind != "user" or principal.user is None:
        raise __import__("fastapi").HTTPException(  # noqa: F405
            status_code=403, detail="service principal cannot change password"
        )
    user = users_svc.change_password(session, principal.user, payload.current_password, payload.new_password)
    return _user_payload(session, user)
