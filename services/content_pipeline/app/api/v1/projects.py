"""Project CRUD endpoints."""

from __future__ import annotations

import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, Query, status
from sqlmodel import Session

from app.api.v1.deps import get_session, require_api_key
from app.schemas.projects import ProjectCreate, ProjectRead, ProjectUpdate
from app.services import projects as svc

router = APIRouter(prefix="/projects", tags=["projects"], dependencies=[Depends(require_api_key)])


@router.post("", response_model=ProjectRead, status_code=status.HTTP_201_CREATED)
def create(payload: ProjectCreate, session: Session = Depends(get_session)) -> ProjectRead:
    return ProjectRead.model_validate(svc.create(session, payload))


@router.get("", response_model=List[ProjectRead])
def list_(
    status_: Optional[str] = Query(default=None, alias="status"),
    limit: int = Query(default=50, le=200, ge=1),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
) -> List[ProjectRead]:
    return [ProjectRead.model_validate(p) for p in svc.list_(session, status_=status_, limit=limit, offset=offset)]


@router.get("/{project_id}", response_model=ProjectRead)
def get(project_id: uuid.UUID, session: Session = Depends(get_session)) -> ProjectRead:
    return ProjectRead.model_validate(svc.get(session, project_id))


@router.patch("/{project_id}", response_model=ProjectRead)
def update(project_id: uuid.UUID, payload: ProjectUpdate, session: Session = Depends(get_session)) -> ProjectRead:
    return ProjectRead.model_validate(svc.update(session, project_id, payload))


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete(project_id: uuid.UUID, session: Session = Depends(get_session)) -> None:
    svc.delete(session, project_id)
