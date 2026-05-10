"""FastAPI dependencies — auth, DB session, project + role gating.

Two auth modes accepted on the same endpoint:
1. **Bearer JWT** (admin panel users) — `Authorization: Bearer <jwt>`
2. **X-API-Key** (workers / cron / ig_scraper bridge / legacy callers)

`require_auth` resolves either to a `Principal` dataclass with a `kind`
of `'user'` or `'service'`. Project-scoped endpoints add `get_project`,
which calls `require_project_member` so a logged-in user without a
membership row gets a 404 (we don't leak existence).

Service principals (X-API-Key) bypass project membership checks — they
represent the system itself.
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from typing import Iterator, Optional

from fastapi import Depends, Header, HTTPException, Path, Request, status
from sqlmodel import Session

from app.core import auth as core_auth
from app.core.config import settings
from app.models.projects import Project
from app.models.users import User
from app.services import users as users_svc
from app.services.database import session_scope


# ---------- principal ----------


@dataclass
class Principal:
    """Authenticated caller — either a logged-in user or the service key."""

    kind: str  # 'user' | 'service'
    user: Optional[User] = None
    role: Optional[str] = None  # global role for users, 'service' for X-API-Key


# ---------- session ----------


def get_session() -> Iterator[Session]:
    """Yield a transactional DB session for request handlers."""
    with session_scope() as session:
        yield session


# ---------- auth ----------


def _check_api_key(x_api_key: Optional[str]) -> bool:
    if not x_api_key:
        return False
    return secrets.compare_digest(x_api_key, settings.CP_API_KEY)


def require_auth(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
    session: Session = Depends(get_session),
) -> Principal:
    """Resolve the caller's principal. 401 if neither Bearer JWT nor API key is valid."""
    # 1. Bearer JWT
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
        try:
            payload = core_auth.decode_access_token(token)
        except core_auth.TokenError as exc:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
        try:
            user_id = uuid.UUID(payload["sub"])
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token subject") from exc
        user = session.get(User, user_id)
        if user is None or user.status != "active":
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="user inactive")
        return Principal(kind="user", user=user, role=user.role)

    # 2. Static service key
    if _check_api_key(x_api_key):
        return Principal(kind="service", user=None, role="service")

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="missing Bearer token or X-API-Key",
        headers={"WWW-Authenticate": "Bearer"},
    )


def require_global_admin(principal: Principal = Depends(require_auth)) -> Principal:
    """Service key counts as admin (it's the system). User must be `role='admin'`."""
    if principal.kind == "service":
        return principal
    if principal.user is None or principal.user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin role required")
    return principal


# Legacy alias — pre-CP-M8.5 endpoints used `require_api_key`. We keep
# the name pointing at the new resolver so no existing routers break.
def require_api_key(_principal: Principal = Depends(require_auth)) -> None:
    """Compat shim — the panel and workers can both use this."""
    return None


# ---------- project scope ----------


def get_project(
    project_id: uuid.UUID = Path(..., description="Project UUID"),
    principal: Principal = Depends(require_auth),
    session: Session = Depends(get_session),
) -> Project:
    """Resolve `{project_id}` path param to a Project row, 404 if missing.

    For user principals: requires a `project_memberships` row (or global
    admin). 404 (not 403) on missing membership so we don't leak project
    existence.
    """
    project = session.get(Project, project_id)
    if project is None or project.status == "archived":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="project not found")

    if principal.kind == "service":
        return project
    if principal.user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="project not found")
    if principal.user.role == "admin":
        return project

    membership = users_svc.get_membership(session, principal.user.id, project.id)
    if membership is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="project not found")
    return project


def require_project_role(min_role: str):
    """Dependency factory: require the caller to have at least `min_role` on the project.

    `service` and global `admin` always pass. For users, the per-project
    role is read from `project_memberships`. Role hierarchy: viewer < editor < owner.
    """
    rank = {"viewer": 0, "editor": 1, "owner": 2}
    if min_role not in rank:
        raise ValueError(f"unknown min_role: {min_role}")
    threshold = rank[min_role]

    def _check(
        project: Project = Depends(get_project),
        principal: Principal = Depends(require_auth),
        session: Session = Depends(get_session),
    ) -> Project:
        if principal.kind == "service":
            return project
        if principal.user is not None and principal.user.role == "admin":
            return project
        if principal.user is None:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")
        membership = users_svc.get_membership(session, principal.user.id, project.id)
        if membership is None or rank.get(membership.role, -1) < threshold:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"role '{min_role}' required on this project",
            )
        return project

    return _check
