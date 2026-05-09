"""Social account CRUD endpoints (publishing accounts)."""

from __future__ import annotations

import uuid
from typing import List

from fastapi import APIRouter, Depends, status
from sqlmodel import Session

from app.api.v1.deps import get_project, get_session, require_api_key
from app.models.projects import Project
from app.schemas.social_accounts import SocialAccountCreate, SocialAccountRead, SocialAccountUpdate
from app.services import social_accounts as svc

router = APIRouter(
    prefix="/projects/{project_id}/social-accounts",
    tags=["social-accounts"],
    dependencies=[Depends(require_api_key)],
)


@router.post("", response_model=SocialAccountRead, status_code=status.HTTP_201_CREATED)
def create(
    payload: SocialAccountCreate,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> SocialAccountRead:
    return svc.create(session, project.id, payload)


@router.get("", response_model=List[SocialAccountRead])
def list_(
    project: Project = Depends(get_project), session: Session = Depends(get_session)
) -> List[SocialAccountRead]:
    return svc.list_(session, project.id)


@router.get("/{account_id}", response_model=SocialAccountRead)
def get(
    account_id: uuid.UUID,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> SocialAccountRead:
    return svc.get(session, project.id, account_id)


@router.patch("/{account_id}", response_model=SocialAccountRead)
def update(
    account_id: uuid.UUID,
    payload: SocialAccountUpdate,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> SocialAccountRead:
    return svc.update(session, project.id, account_id, payload)


@router.delete("/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete(
    account_id: uuid.UUID,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> None:
    svc.delete(session, project.id, account_id)
