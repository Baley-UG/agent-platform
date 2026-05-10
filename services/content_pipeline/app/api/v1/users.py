"""Users + project memberships management.

User CRUD is **global-admin only**. Adding a user to a project requires
`owner` on that project (or global admin).
"""

from __future__ import annotations

import uuid
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session

from app.api.v1.deps import (
    Principal,
    get_project,
    get_session,
    require_auth,
    require_global_admin,
    require_project_role,
)
from app.models.projects import Project
from app.schemas.auth import (
    MembershipCreate,
    MembershipUpdate,
    ProjectMembershipRead,
    UserCreate,
    UserRead,
    UserUpdate,
)
from app.services import users as svc

# ---- Global users management (admin only) ----
router = APIRouter(
    prefix="/users",
    tags=["users"],
    dependencies=[Depends(require_global_admin)],
)


def _to_read(session: Session, user) -> UserRead:
    memberships = svc.memberships_for_user(session, user.id)
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


@router.post("", response_model=UserRead, status_code=status.HTTP_201_CREATED)
def create_user(payload: UserCreate, session: Session = Depends(get_session)) -> UserRead:
    user = svc.create(
        session, email=payload.email, password=payload.password, name=payload.name, role=payload.role
    )
    return _to_read(session, user)


@router.get("", response_model=List[UserRead])
def list_users(session: Session = Depends(get_session)) -> List[UserRead]:
    return [_to_read(session, u) for u in svc.list_(session)]


@router.get("/{user_id}", response_model=UserRead)
def get_user(user_id: uuid.UUID, session: Session = Depends(get_session)) -> UserRead:
    return _to_read(session, svc.get(session, user_id))


@router.patch("/{user_id}", response_model=UserRead)
def update_user(
    user_id: uuid.UUID, payload: UserUpdate, session: Session = Depends(get_session)
) -> UserRead:
    user = svc.get(session, user_id)
    user = svc.update(
        session,
        user,
        name=payload.name,
        role=payload.role,
        status_=payload.status,
        password=payload.password,
    )
    return _to_read(session, user)


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(user_id: uuid.UUID, session: Session = Depends(get_session)) -> None:
    user = svc.get(session, user_id)
    svc.delete(session, user)


# ---- Project membership management (project owner or global admin) ----
membership_router = APIRouter(
    prefix="/projects/{project_id}/members",
    tags=["project-members"],
)


def _membership_to_read(m) -> ProjectMembershipRead:
    return ProjectMembershipRead.model_validate(m)


@membership_router.get(
    "",
    response_model=List[dict],
    dependencies=[Depends(require_auth)],
)
def list_members(
    project: Project = Depends(get_project), session: Session = Depends(get_session)
) -> List[dict]:
    """List users on this project (any member can see the roster)."""
    out = []
    for m in svc.memberships_for_project(session, project.id):
        u = svc.get(session, m.user_id)
        out.append(
            {
                "membership_id": str(m.id),
                "user_id": str(u.id),
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
    payload: MembershipCreate,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> ProjectMembershipRead:
    # User must exist.
    svc.get(session, payload.user_id)
    membership = svc.add_membership(session, payload.user_id, project.id, payload.role)
    return _membership_to_read(membership)


@membership_router.patch(
    "/{user_id}",
    response_model=ProjectMembershipRead,
    dependencies=[Depends(require_project_role("owner"))],
)
def update_member(
    user_id: uuid.UUID,
    payload: MembershipUpdate,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> ProjectMembershipRead:
    membership = svc.get_membership(session, user_id, project.id)
    if membership is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="membership not found")
    return _membership_to_read(svc.update_membership(session, membership, payload.role))


@membership_router.delete(
    "/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_project_role("owner"))],
)
def remove_member(
    user_id: uuid.UUID,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> None:
    membership = svc.get_membership(session, user_id, project.id)
    if membership is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="membership not found")
    svc.remove_membership(session, membership)
