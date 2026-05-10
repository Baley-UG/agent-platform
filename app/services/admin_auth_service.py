"""Admin auth + users + memberships service layer.

Lives in main `app/`. The admin panel is the only consumer; downstream
microservices (content_pipeline, ig_scraper) don't talk to this.

Token rotation: every `/admin/auth/refresh` revokes the consumed
`admin_session` row and issues a fresh pair (replay defense).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import List, Optional, Tuple
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.core.logging import logger
from app.models.admin_session import AdminSession
from app.models.project_membership import ProjectMembership
from app.models.user import User
from app.utils import admin_auth as core


# ---------- users ----------


def get_user_by_email(session: Session, email: str) -> Optional[User]:
    return session.exec(select(User).where(User.email == email.strip().lower())).first()


def get_user(session: Session, user_id: int) -> User:
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")
    return user


def list_users(session: Session, *, limit: int = 100, offset: int = 0) -> List[User]:
    stmt = select(User).order_by(User.created_at.desc()).limit(limit).offset(offset)
    return list(session.exec(stmt).all())


def create_user(
    session: Session, *, email: str, password: str, name: Optional[str] = None, role: str = "member"
) -> User:
    if len(password) < 8:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="password must be 8+ chars")
    user = User(
        email=email.strip().lower(),
        hashed_password=User.hash_password(password),
        name=name,
        role=role,
    )
    session.add(user)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="email already exists") from exc
    session.refresh(user)
    return user


def update_user(
    session: Session,
    user: User,
    *,
    name: Optional[str] = None,
    role: Optional[str] = None,
    status_: Optional[str] = None,
    password: Optional[str] = None,
) -> User:
    if name is not None:
        user.name = name
    if role is not None:
        user.role = role
    if status_ is not None:
        user.status = status_
    if password is not None:
        if len(password) < 8:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="password must be 8+ chars"
            )
        user.hashed_password = User.hash_password(password)
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def delete_user(session: Session, user: User) -> None:
    session.delete(user)
    session.commit()


def change_password(session: Session, user: User, current: str, new: str) -> User:
    if not user.verify_password(current):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="current password incorrect")
    return update_user(session, user, password=new)


# ---------- login / refresh / logout ----------


def login(
    session: Session,
    *,
    email: str,
    password: str,
    user_agent: Optional[str] = None,
    ip: Optional[str] = None,
) -> Tuple[User, str, str, datetime]:
    user = get_user_by_email(session, email)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials")
    if user.status != "active":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="account disabled")
    if not user.verify_password(password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials")

    user.last_login_at = datetime.now(UTC)
    session.add(user)

    access_token, access_exp = core.issue_access_token(user.id, user.role)
    raw_refresh, refresh_hash, refresh_exp = core.issue_refresh_token()
    session.add(
        AdminSession(
            user_id=user.id,
            token_hash=refresh_hash,
            expires_at=refresh_exp,
            user_agent=(user_agent or "")[:512] or None,
            ip=(ip or "")[:64] or None,
        )
    )
    session.commit()
    session.refresh(user)
    return user, access_token, raw_refresh, access_exp


def refresh(
    session: Session,
    *,
    raw_refresh: str,
    user_agent: Optional[str] = None,
    ip: Optional[str] = None,
) -> Tuple[User, str, str, datetime]:
    token_hash = core.hash_refresh_token(raw_refresh)
    row = session.exec(select(AdminSession).where(AdminSession.token_hash == token_hash)).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid refresh token")
    if row.revoked_at is not None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="refresh token revoked")
    if row.expires_at < datetime.now(UTC):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="refresh token expired")

    user = session.get(User, row.user_id)
    if user is None or user.status != "active":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="account unavailable")

    row.revoked_at = datetime.now(UTC)
    session.add(row)

    access_token, access_exp = core.issue_access_token(user.id, user.role)
    new_raw, new_hash, new_exp = core.issue_refresh_token()
    session.add(
        AdminSession(
            user_id=user.id,
            token_hash=new_hash,
            expires_at=new_exp,
            user_agent=(user_agent or "")[:512] or None,
            ip=(ip or "")[:64] or None,
        )
    )
    session.commit()
    return user, access_token, new_raw, access_exp


def logout(session: Session, *, raw_refresh: str) -> None:
    token_hash = core.hash_refresh_token(raw_refresh)
    row = session.exec(select(AdminSession).where(AdminSession.token_hash == token_hash)).first()
    if row is None or row.revoked_at is not None:
        return
    row.revoked_at = datetime.now(UTC)
    session.add(row)
    session.commit()


# ---------- project memberships ----------


def memberships_for_user(session: Session, user_id: int) -> List[ProjectMembership]:
    return list(
        session.exec(select(ProjectMembership).where(ProjectMembership.user_id == user_id)).all()
    )


def memberships_for_project(session: Session, project_id: UUID) -> List[ProjectMembership]:
    return list(
        session.exec(select(ProjectMembership).where(ProjectMembership.project_id == project_id)).all()
    )


def get_membership(session: Session, user_id: int, project_id: UUID) -> Optional[ProjectMembership]:
    return session.exec(
        select(ProjectMembership).where(
            ProjectMembership.user_id == user_id,
            ProjectMembership.project_id == project_id,
        )
    ).first()


def add_membership(session: Session, user_id: int, project_id: UUID, role: str) -> ProjectMembership:
    row = ProjectMembership(user_id=user_id, project_id=project_id, role=role)
    session.add(row)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="membership already exists") from exc
    session.refresh(row)
    return row


def update_membership(session: Session, membership: ProjectMembership, role: str) -> ProjectMembership:
    membership.role = role
    session.add(membership)
    session.commit()
    session.refresh(membership)
    return membership


def remove_membership(session: Session, membership: ProjectMembership) -> None:
    session.delete(membership)
    session.commit()


# ---------- bootstrap ----------


def ensure_bootstrap_admin(session: Session, *, email: str, password: str, name: Optional[str] = None) -> Optional[User]:
    """Create the first admin if NO admin users exist yet.

    Idempotent — once any admin exists this is a no-op even when env
    credentials change. Removing an admin then restarting WILL recreate
    one, but that's a development-only edge case.
    """
    if not email or not password:
        return None
    has_admin = session.exec(select(User).where(User.role == "admin").limit(1)).first()
    if has_admin is not None:
        return None
    try:
        user = create_user(session, email=email, password=password, name=name or "Admin", role="admin")
        logger.info("bootstrap_admin_created", email=email)
        return user
    except Exception as exc:  # noqa: BLE001
        logger.warning("bootstrap_admin_failed", error=str(exc))
        return None
