"""Template CRUD endpoints."""

from __future__ import annotations

import uuid
from typing import List

from fastapi import APIRouter, Depends, status
from sqlmodel import Session

from app.api.v1.deps import get_project, get_session, require_api_key
from app.models.projects import Project
from app.schemas.templates import TemplateCreate, TemplateRead, TemplateUpdate
from app.services import templates as svc

router = APIRouter(
    prefix="/projects/{project_id}/templates",
    tags=["templates"],
    dependencies=[Depends(require_api_key)],
)


@router.post("", response_model=TemplateRead, status_code=status.HTTP_201_CREATED)
def create(
    payload: TemplateCreate,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> TemplateRead:
    return TemplateRead.model_validate(svc.create(session, project.id, payload))


@router.get("", response_model=List[TemplateRead])
def list_(project: Project = Depends(get_project), session: Session = Depends(get_session)) -> List[TemplateRead]:
    return [TemplateRead.model_validate(t) for t in svc.list_(session, project.id)]


@router.get("/{template_id}", response_model=TemplateRead)
def get(
    template_id: uuid.UUID,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> TemplateRead:
    return TemplateRead.model_validate(svc.get(session, project.id, template_id))


@router.patch("/{template_id}", response_model=TemplateRead)
def update(
    template_id: uuid.UUID,
    payload: TemplateUpdate,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> TemplateRead:
    return TemplateRead.model_validate(svc.update(session, project.id, template_id, payload))


@router.delete("/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete(
    template_id: uuid.UUID,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> None:
    svc.delete(session, project.id, template_id)
