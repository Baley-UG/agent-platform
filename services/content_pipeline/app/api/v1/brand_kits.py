"""Brand kit CRUD endpoints."""

from __future__ import annotations

import uuid
from typing import List

from fastapi import APIRouter, Depends, status
from sqlmodel import Session

from app.api.v1.deps import get_project, get_session, require_api_key
from app.models.projects import Project
from app.schemas.brand_kits import BrandKitCreate, BrandKitRead, BrandKitUpdate
from app.services import brand_kits as svc

router = APIRouter(
    prefix="/projects/{project_id}/brand-kits",
    tags=["brand-kits"],
    dependencies=[Depends(require_api_key)],
)


@router.post("", response_model=BrandKitRead, status_code=status.HTTP_201_CREATED)
def create(
    payload: BrandKitCreate,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> BrandKitRead:
    return BrandKitRead.model_validate(svc.create(session, project.id, payload))


@router.get("", response_model=List[BrandKitRead])
def list_(project: Project = Depends(get_project), session: Session = Depends(get_session)) -> List[BrandKitRead]:
    return [BrandKitRead.model_validate(k) for k in svc.list_(session, project.id)]


@router.get("/{kit_id}", response_model=BrandKitRead)
def get(
    kit_id: uuid.UUID, project: Project = Depends(get_project), session: Session = Depends(get_session)
) -> BrandKitRead:
    return BrandKitRead.model_validate(svc.get(session, project.id, kit_id))


@router.patch("/{kit_id}", response_model=BrandKitRead)
def update(
    kit_id: uuid.UUID,
    payload: BrandKitUpdate,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> BrandKitRead:
    return BrandKitRead.model_validate(svc.update(session, project.id, kit_id, payload))


@router.delete("/{kit_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete(
    kit_id: uuid.UUID, project: Project = Depends(get_project), session: Session = Depends(get_session)
) -> None:
    svc.delete(session, project.id, kit_id)
