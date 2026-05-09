"""Project CRUD service."""

from __future__ import annotations

import uuid
from typing import List, Optional

from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.models.projects import Project
from app.schemas.projects import ProjectCreate, ProjectUpdate


def create(session: Session, payload: ProjectCreate) -> Project:
    project = Project(
        slug=payload.slug,
        name=payload.name,
        reuse_policy=payload.reuse_policy,
        weekly_budget_cap_usd=payload.weekly_budget_cap_usd,
    )
    session.add(project)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"project slug already exists: {payload.slug}") from exc
    session.refresh(project)
    return project


def list_(session: Session, status_: Optional[str] = None, limit: int = 100, offset: int = 0) -> List[Project]:
    stmt = select(Project)
    if status_:
        stmt = stmt.where(Project.status == status_)
    stmt = stmt.order_by(Project.created_at.desc()).limit(limit).offset(offset)
    return list(session.exec(stmt).all())


def get(session: Session, project_id: uuid.UUID) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="project not found")
    return project


def get_by_slug(session: Session, slug: str) -> Optional[Project]:
    return session.exec(select(Project).where(Project.slug == slug)).first()


def update(session: Session, project_id: uuid.UUID, payload: ProjectUpdate) -> Project:
    project = get(session, project_id)
    data = payload.model_dump(exclude_unset=True)
    for key, value in data.items():
        setattr(project, key, value)
    session.add(project)
    session.flush()
    session.refresh(project)
    return project


def delete(session: Session, project_id: uuid.UUID) -> None:
    """Soft delete: status='archived'. Keeps data for audit / accidental restore."""
    project = get(session, project_id)
    project.status = "archived"
    session.add(project)
    session.flush()
