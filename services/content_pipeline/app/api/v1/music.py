"""Music track CRUD endpoints."""

from __future__ import annotations

import uuid
from typing import List

from fastapi import APIRouter, Depends, status
from sqlmodel import Session

from app.api.v1.deps import get_project, get_session, require_api_key
from app.models.projects import Project
from app.schemas.music import MusicTrackCreate, MusicTrackRead, MusicTrackUpdate
from app.services import music as svc

router = APIRouter(
    prefix="/projects/{project_id}/music-tracks",
    tags=["music"],
    dependencies=[Depends(require_api_key)],
)


@router.post("", response_model=MusicTrackRead, status_code=status.HTTP_201_CREATED)
def create(
    payload: MusicTrackCreate,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> MusicTrackRead:
    return MusicTrackRead.model_validate(svc.create(session, project.id, payload))


@router.get("", response_model=List[MusicTrackRead])
def list_(project: Project = Depends(get_project), session: Session = Depends(get_session)) -> List[MusicTrackRead]:
    return [MusicTrackRead.model_validate(t) for t in svc.list_(session, project.id)]


@router.get("/{track_id}", response_model=MusicTrackRead)
def get(
    track_id: uuid.UUID,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> MusicTrackRead:
    return MusicTrackRead.model_validate(svc.get(session, project.id, track_id))


@router.patch("/{track_id}", response_model=MusicTrackRead)
def update(
    track_id: uuid.UUID,
    payload: MusicTrackUpdate,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> MusicTrackRead:
    return MusicTrackRead.model_validate(svc.update(session, project.id, track_id, payload))


@router.delete("/{track_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete(
    track_id: uuid.UUID,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> None:
    svc.delete(session, project.id, track_id)
