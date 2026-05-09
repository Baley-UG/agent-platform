"""Template CRUD service."""

from __future__ import annotations

import uuid
from typing import List

from fastapi import HTTPException, status
from sqlmodel import Session, select

from app.models.templates import Template
from app.schemas.templates import TemplateCreate, TemplateUpdate


def create(session: Session, project_id: uuid.UUID, payload: TemplateCreate) -> Template:
    row = Template(project_id=project_id, **payload.model_dump())
    session.add(row)
    session.flush()
    session.refresh(row)
    return row


def list_(session: Session, project_id: uuid.UUID) -> List[Template]:
    stmt = select(Template).where(Template.project_id == project_id).order_by(Template.created_at.desc())
    return list(session.exec(stmt).all())


def get(session: Session, project_id: uuid.UUID, template_id: uuid.UUID) -> Template:
    row = session.get(Template, template_id)
    if row is None or row.project_id != project_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="template not found")
    return row


def update(session: Session, project_id: uuid.UUID, template_id: uuid.UUID, payload: TemplateUpdate) -> Template:
    row = get(session, project_id, template_id)
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(row, key, value)
    session.add(row)
    session.flush()
    session.refresh(row)
    return row


def delete(session: Session, project_id: uuid.UUID, template_id: uuid.UUID) -> None:
    row = get(session, project_id, template_id)
    session.delete(row)
    session.flush()
