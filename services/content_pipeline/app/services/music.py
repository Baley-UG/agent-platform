"""Music track CRUD service."""

from __future__ import annotations

import uuid
from typing import List

from fastapi import HTTPException, status
from sqlmodel import Session, select

from app.models.music import MusicTrack
from app.schemas.music import MusicTrackCreate, MusicTrackUpdate


def create(session: Session, project_id: uuid.UUID, payload: MusicTrackCreate) -> MusicTrack:
    row = MusicTrack(project_id=project_id, **payload.model_dump())
    session.add(row)
    session.flush()
    session.refresh(row)
    return row


def list_(session: Session, project_id: uuid.UUID) -> List[MusicTrack]:
    stmt = select(MusicTrack).where(MusicTrack.project_id == project_id).order_by(MusicTrack.created_at.desc())
    return list(session.exec(stmt).all())


def get(session: Session, project_id: uuid.UUID, track_id: uuid.UUID) -> MusicTrack:
    row = session.get(MusicTrack, track_id)
    if row is None or row.project_id != project_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="music_track not found")
    return row


def update(session: Session, project_id: uuid.UUID, track_id: uuid.UUID, payload: MusicTrackUpdate) -> MusicTrack:
    row = get(session, project_id, track_id)
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(row, key, value)
    session.add(row)
    session.flush()
    session.refresh(row)
    return row


def delete(session: Session, project_id: uuid.UUID, track_id: uuid.UUID) -> None:
    row = get(session, project_id, track_id)
    session.delete(row)
    session.flush()
