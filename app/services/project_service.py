"""Project CRUD service.

Lives in main app because `projects` is the platform-wide tenant root —
sub-resources (brand_kits, scenarios, plan_slots, etc.) remain owned by
content_pipeline, which reads `public.projects` cross-schema. Admin
panel hits these endpoints for the project lifecycle; everything else
still goes through the gateway to the appropriate downstream.

Access rules:
    - List: admins see everything; members see only projects they're
      a member of (via `project_membership`). Returned shape is the same.
    - Read/update: admin OR membership with role >= viewer.
    - Create/delete: admin only.

Slug uniqueness is enforced at the DB level; 409 on collision.
"""

from __future__ import annotations

from typing import List, Optional
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.models.project import Project
from app.models.project_membership import ProjectMembership
from app.schemas.project import ProjectCreate, ProjectUpdate


def _get_or_404(session: Session, project_id: UUID) -> Project:
    p = session.get(Project, project_id)
    if p is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="project not found")
    return p


def list_projects(
    session: Session,
    *,
    actor_user_id: int,
    actor_role: str,
    limit: int = 100,
    offset: int = 0,
) -> List[Project]:
    """Admin → all projects; non-admin → only via membership."""
    stmt = select(Project).order_by(Project.created_at.desc())
    if actor_role != "admin":
        stmt = stmt.join(
            ProjectMembership, ProjectMembership.project_id == Project.id
        ).where(ProjectMembership.user_id == actor_user_id)
    stmt = stmt.limit(limit).offset(offset)
    return list(session.exec(stmt).all())


def get_project(
    session: Session,
    project_id: UUID,
    *,
    actor_user_id: int,
    actor_role: str,
) -> Project:
    p = _get_or_404(session, project_id)
    if actor_role == "admin":
        return p
    membership = session.exec(
        select(ProjectMembership).where(
            ProjectMembership.user_id == actor_user_id,
            ProjectMembership.project_id == project_id,
        )
    ).first()
    if membership is None:
        # 404, not 403 — don't leak existence to unauthorised users.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="project not found")
    return p


def create_project(session: Session, payload: ProjectCreate) -> Project:
    project = Project(
        slug=payload.slug,
        name=payload.name,
        status=payload.status,
        reuse_policy=payload.reuse_policy,
        weekly_budget_cap_usd=(
            float(payload.weekly_budget_cap_usd)
            if payload.weekly_budget_cap_usd is not None
            else None
        ),
    )
    session.add(project)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"slug '{payload.slug}' already exists",
        ) from exc
    session.refresh(project)
    return project


def update_project(
    session: Session,
    project: Project,
    payload: ProjectUpdate,
) -> Project:
    data = payload.model_dump(exclude_unset=True)
    if "weekly_budget_cap_usd" in data and data["weekly_budget_cap_usd"] is not None:
        data["weekly_budget_cap_usd"] = float(data["weekly_budget_cap_usd"])
    for field, value in data.items():
        setattr(project, field, value)
    session.add(project)
    session.commit()
    session.refresh(project)
    return project


def delete_project(session: Session, project: Project) -> None:
    """Hard delete. The FK from project_membership and content_pipeline
    tables uses CASCADE, so every downstream row keyed on this project
    is removed in the same transaction.
    """
    session.delete(project)
    session.commit()


def archive_project(session: Session, project: Project) -> Project:
    """Soft-delete shortcut — flips status to 'archived' instead of
    physically removing the row. Useful when you want to retain history
    (e.g. cost ledger) but hide the project from active listings.
    """
    project.status = "archived"
    session.add(project)
    session.commit()
    session.refresh(project)
    return project
