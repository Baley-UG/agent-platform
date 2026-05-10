"""Admin user + membership management.

Global-admin-only. Users are managed at /admin/users; memberships per
project at /admin/projects/{pid}/members.
"""

from __future__ import annotations

from typing import List
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session

from app.api.v1.admin_deps import (
    AdminPrincipal,
    get_db,
    require_admin_token,
    require_global_admin,
    require_project_role,
)
from app.schemas.admin import (
    AdminUserRead,
    MembershipCreateRequest,
    MembershipUpdateRequest,
    ProjectMembershipRead,
    UserCreateRequest,
    UserUpdateRequest,
)
from app.services import admin_auth_service as svc

users_router = APIRouter(
    prefix="/admin/users", tags=["admin-users"], dependencies=[Depends(require_global_admin)]
)


def _to_read(session: Session, user) -> AdminUserRead:
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


@users_router.post("", response_model=AdminUserRead, status_code=status.HTTP_201_CREATED)
def create_user(payload: UserCreateRequest, session: Session = Depends(get_db)) -> AdminUserRead:
    user = svc.create_user(
        session, email=payload.email, password=payload.password, name=payload.name, role=payload.role
    )
    return _to_read(session, user)


@users_router.get("", response_model=List[AdminUserRead])
def list_users(session: Session = Depends(get_db)) -> List[AdminUserRead]:
    return [_to_read(session, u) for u in svc.list_users(session)]


@users_router.get("/{user_id}", response_model=AdminUserRead)
def get_user(user_id: int, session: Session = Depends(get_db)) -> AdminUserRead:
    return _to_read(session, svc.get_user(session, user_id))


@users_router.patch("/{user_id}", response_model=AdminUserRead)
def update_user(
    user_id: int, payload: UserUpdateRequest, session: Session = Depends(get_db)
) -> AdminUserRead:
    user = svc.get_user(session, user_id)
    user = svc.update_user(
        session,
        user,
        name=payload.name,
        role=payload.role,
        status_=payload.status,
        password=payload.password,
    )
    return _to_read(session, user)


@users_router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(user_id: int, session: Session = Depends(get_db)) -> None:
    svc.delete_user(session, svc.get_user(session, user_id))


# ---- Memberships per project ----
membership_router = APIRouter(prefix="/admin/projects/{project_id}/members", tags=["admin-memberships"])


@membership_router.get(
    "",
    response_model=List[dict],
    dependencies=[Depends(require_admin_token)],
)
def list_members(project_id: UUID, session: Session = Depends(get_db)) -> List[dict]:
    out = []
    for m in svc.memberships_for_project(session, project_id):
        u = svc.get_user(session, m.user_id)
        out.append(
            {
                "membership_id": m.id,
                "user_id": u.id,
                "email": u.email,
                "name": u.name,
                "role": m.role,
                "status": u.status,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
        )
    return out


@membership_router.post(
    "",
    response_model=ProjectMembershipRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_project_role("owner"))],
)
def add_member(
    project_id: UUID, payload: MembershipCreateRequest, session: Session = Depends(get_db)
) -> ProjectMembershipRead:
    svc.get_user(session, payload.user_id)
    return ProjectMembershipRead.model_validate(
        svc.add_membership(session, payload.user_id, project_id, payload.role)
    )


@membership_router.patch(
    "/{user_id}",
    response_model=ProjectMembershipRead,
    dependencies=[Depends(require_project_role("owner"))],
)
def update_member(
    project_id: UUID,
    user_id: int,
    payload: MembershipUpdateRequest,
    session: Session = Depends(get_db),
) -> ProjectMembershipRead:
    membership = svc.get_membership(session, user_id, project_id)
    if membership is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="membership not found")
    return ProjectMembershipRead.model_validate(svc.update_membership(session, membership, payload.role))


@membership_router.delete(
    "/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_project_role("owner"))],
)
def remove_member(project_id: UUID, user_id: int, session: Session = Depends(get_db)) -> None:
    membership = svc.get_membership(session, user_id, project_id)
    if membership is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="membership not found")
    svc.remove_membership(session, membership)
