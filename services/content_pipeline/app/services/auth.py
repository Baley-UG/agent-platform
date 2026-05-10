"""Login + refresh + logout flows.

Each successful login creates an `auth_sessions` row holding the SHA-256
of a fresh refresh token. Refresh exchanges the raw token for a new
access token (and rotates the refresh token — the old one is revoked).
Logout revokes the row.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional, Tuple

from fastapi import HTTPException, status
from sqlmodel import Session, select

from app.core import auth as core_auth
from app.models.sessions import AuthSession
from app.models.users import User
from app.services import users as users_svc


def login(
    session: Session,
    *,
    email: str,
    password: str,
    user_agent: Optional[str] = None,
    ip: Optional[str] = None,
) -> Tuple[User, str, str, datetime]:
    """Returns (user, access_token, refresh_token_raw, access_expires_at)."""
    user = users_svc.get_by_email(session, email)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials")
    if user.status != "active":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="account disabled")
    if not core_auth.verify_password(password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials")

    # Opportunistic rehash if the parameters drifted.
    if core_auth.needs_rehash(user.password_hash):
        user.password_hash = core_auth.hash_password(password)

    user.last_login_at = datetime.now(timezone.utc)
    session.add(user)

    access_token, access_exp = core_auth.issue_access_token(user.id, user.role)
    raw_refresh, refresh_hash, refresh_exp = core_auth.issue_refresh_token()
    session.add(
        AuthSession(
            user_id=user.id,
            token_hash=refresh_hash,
            expires_at=refresh_exp,
            user_agent=(user_agent or "")[:512] or None,
            ip=(ip or "")[:64] or None,
        )
    )
    session.flush()
    return user, access_token, raw_refresh, access_exp


def refresh(
    session: Session,
    *,
    raw_refresh: str,
    user_agent: Optional[str] = None,
    ip: Optional[str] = None,
) -> Tuple[User, str, str, datetime]:
    """Rotate refresh token + issue a new access token."""
    token_hash = core_auth.hash_refresh_token(raw_refresh)
    row = session.exec(
        select(AuthSession).where(AuthSession.token_hash == token_hash)
    ).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid refresh token")
    if row.revoked_at is not None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="refresh token revoked")
    if row.expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="refresh token expired")

    user = session.get(User, row.user_id)
    if user is None or user.status != "active":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="account unavailable")

    # Rotate the refresh token: revoke this row, issue a new one.
    row.revoked_at = datetime.now(timezone.utc)
    session.add(row)

    access_token, access_exp = core_auth.issue_access_token(user.id, user.role)
    new_raw, new_hash, new_exp = core_auth.issue_refresh_token()
    session.add(
        AuthSession(
            user_id=user.id,
            token_hash=new_hash,
            expires_at=new_exp,
            user_agent=(user_agent or "")[:512] or None,
            ip=(ip or "")[:64] or None,
        )
    )
    session.flush()
    return user, access_token, new_raw, access_exp


def logout(session: Session, *, raw_refresh: str) -> None:
    """Revoke the refresh row matching this token. Idempotent (404-on-miss is silent)."""
    token_hash = core_auth.hash_refresh_token(raw_refresh)
    row = session.exec(
        select(AuthSession).where(AuthSession.token_hash == token_hash)
    ).first()
    if row is None or row.revoked_at is not None:
        return
    row.revoked_at = datetime.now(timezone.utc)
    session.add(row)
    session.flush()
