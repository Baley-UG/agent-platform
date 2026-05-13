"""`/admin/projects` — platform-wide project CRUD.

Project is the multi-tenancy root for the whole platform (main app +
ig_scraper + content_pipeline + future TikTok), so its CRUD doesn't
belong inside any single downstream service. content_pipeline keeps
owning the sub-resources (`/cp/projects/{pid}/brand-kits`, `.../scenarios`,
etc.), which join `public.projects` via cross-schema FKs.

Access:
    GET    /admin/projects            — any authenticated user (filtered by membership)
    GET    /admin/projects/{pid}      — admin OR any membership
    POST   /admin/projects            — admin only
    PATCH  /admin/projects/{pid}      — admin OR any membership
    POST   /admin/projects/{pid}/archive  — admin only (soft delete)
    DELETE /admin/projects/{pid}      — admin only (hard delete; cascades)
"""

from __future__ import annotations

from typing import List
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlmodel import Session

from app.api.v1.admin_deps import (
    AdminPrincipal,
    get_db,
    require_admin_token,
    require_global_admin,
)
from app.schemas.project import ProjectCreate, ProjectRead, ProjectUpdate
from app.services import project_service as svc

router = APIRouter(prefix="/admin/projects", tags=["admin-projects"])


@router.get(
    "",
    response_model=List[ProjectRead],
    summary="List projects (filtered by membership for non-admins)",
)
def list_projects(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    principal: AdminPrincipal = Depends(require_admin_token),
    session: Session = Depends(get_db),
) -> List[ProjectRead]:
    rows = svc.list_projects(
        session,
        actor_user_id=principal.user.id,
        actor_role=principal.role,
        limit=limit,
        offset=offset,
    )
    return [ProjectRead.model_validate(r) for r in rows]


@router.get(
    "/{project_id}",
    response_model=ProjectRead,
    responses={404: {"description": "project not found / no membership"}},
)
def get_project(
    project_id: UUID,
    principal: AdminPrincipal = Depends(require_admin_token),
    session: Session = Depends(get_db),
) -> ProjectRead:
    row = svc.get_project(
        session,
        project_id,
        actor_user_id=principal.user.id,
        actor_role=principal.role,
    )
    return ProjectRead.model_validate(row)


@router.post(
    "",
    response_model=ProjectRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new project (admin only)",
    responses={409: {"description": "slug already exists"}},
)
def create_project(
    payload: ProjectCreate,
    _principal: AdminPrincipal = Depends(require_global_admin),
    session: Session = Depends(get_db),
) -> ProjectRead:
    row = svc.create_project(session, payload)
    return ProjectRead.model_validate(row)


@router.patch(
    "/{project_id}",
    response_model=ProjectRead,
    responses={404: {"description": "project not found / no membership"}},
)
def update_project(
    project_id: UUID,
    payload: ProjectUpdate,
    principal: AdminPrincipal = Depends(require_admin_token),
    session: Session = Depends(get_db),
) -> ProjectRead:
    row = svc.get_project(
        session,
        project_id,
        actor_user_id=principal.user.id,
        actor_role=principal.role,
    )
    row = svc.update_project(session, row, payload)
    return ProjectRead.model_validate(row)


@router.post(
    "/{project_id}/archive",
    response_model=ProjectRead,
    summary="Soft-delete (status=archived). Reversible.",
)
def archive_project(
    project_id: UUID,
    _principal: AdminPrincipal = Depends(require_global_admin),
    session: Session = Depends(get_db),
) -> ProjectRead:
    project = svc._get_or_404(session, project_id)
    return ProjectRead.model_validate(svc.archive_project(session, project))


@router.delete(
    "/{project_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Hard-delete the project. Cascades to ALL sub-resources.",
    responses={404: {"description": "project not found"}},
)
def delete_project(
    project_id: UUID,
    _principal: AdminPrincipal = Depends(require_global_admin),
    session: Session = Depends(get_db),
) -> None:
    project = svc._get_or_404(session, project_id)
    svc.delete_project(session, project)
