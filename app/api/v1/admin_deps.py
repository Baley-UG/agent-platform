"""Admin-only dependencies (Bearer JWT auth + role gates).

Distinct from the chatbot auth in `app/api/v1/auth.py:get_current_user`,
which validates chat-thread tokens. These dependencies validate
**admin** access tokens issued by `app/services/admin_auth_service.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from uuid import UUID

from fastapi import Depends, Header, HTTPException, Path, status
from sqlmodel import Session

from app.models.user import User
from app.services import admin_auth_service as svc
from app.services.database import database_service
from app.utils import admin_auth as core


@dataclass
class AdminPrincipal:
    user: User
    role: str  # global role


def get_db():
    """Yield a Session bound to the singleton engine."""
    with Session(database_service.engine) as session:
        yield session


def require_admin_token(
    authorization: Optional[str] = Header(default=None),
    session: Session = Depends(get_db),
) -> AdminPrincipal:
    """Validate Authorization: Bearer <admin_access>. Returns the user."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing Bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization.split(" ", 1)[1].strip()
    try:
        payload = core.decode_access_token(token)
    except core.AdminTokenError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

    try:
        user_id = int(payload["sub"])
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token subject") from exc

    user = session.get(User, user_id)
    if user is None or user.status != "active":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="user inactive")
    return AdminPrincipal(user=user, role=user.role)


def require_global_admin(principal: AdminPrincipal = Depends(require_admin_token)) -> AdminPrincipal:
    if principal.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin role required")
    return principal


_PROJECT_ROLE_RANK = {"viewer": 0, "editor": 1, "owner": 2}


def require_project_role(min_role: str):
    """Dependency factory: caller must have at least `min_role` on this project.

    Global admin always passes. For non-admin members, project_membership
    is consulted; missing membership → 404 (don't leak existence).
    """
    if min_role not in _PROJECT_ROLE_RANK:
        raise ValueError(f"unknown min_role: {min_role}")
    threshold = _PROJECT_ROLE_RANK[min_role]

    def _check(
        project_id: UUID = Path(...),
        principal: AdminPrincipal = Depends(require_admin_token),
        session: Session = Depends(get_db),
    ) -> UUID:
        if principal.role == "admin":
            return project_id
        membership = svc.get_membership(session, principal.user.id, project_id)
        if membership is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="project not found")
        if _PROJECT_ROLE_RANK.get(membership.role, -1) < threshold:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"role '{min_role}' required on this project",
            )
        return project_id

    return _check


def require_project_access(
    project_id: UUID = Path(...),
    principal: AdminPrincipal = Depends(require_admin_token),
    session: Session = Depends(get_db),
) -> UUID:
    """Any membership level (or global admin) is enough."""
    if principal.role == "admin":
        return project_id
    if svc.get_membership(session, principal.user.id, project_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="project not found")
    return project_id
