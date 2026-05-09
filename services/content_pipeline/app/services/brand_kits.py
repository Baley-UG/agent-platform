"""Brand kit CRUD service."""

from __future__ import annotations

import uuid
from typing import List

from fastapi import HTTPException, status
from sqlalchemy import update as sql_update
from sqlmodel import Session, select

from app.models.brand_kits import BrandKit
from app.schemas.brand_kits import BrandKitCreate, BrandKitUpdate


def _unset_defaults_for_project(session: Session, project_id: uuid.UUID, except_id: uuid.UUID | None = None) -> None:
    stmt = sql_update(BrandKit).where(BrandKit.project_id == project_id, BrandKit.is_default == True)  # noqa: E712
    if except_id is not None:
        stmt = stmt.where(BrandKit.id != except_id)
    stmt = stmt.values(is_default=False)
    session.exec(stmt)


def create(session: Session, project_id: uuid.UUID, payload: BrandKitCreate) -> BrandKit:
    kit = BrandKit(project_id=project_id, **payload.model_dump())
    session.add(kit)
    session.flush()
    if kit.is_default:
        _unset_defaults_for_project(session, project_id, except_id=kit.id)
    session.refresh(kit)
    return kit


def list_(session: Session, project_id: uuid.UUID) -> List[BrandKit]:
    stmt = select(BrandKit).where(BrandKit.project_id == project_id).order_by(BrandKit.created_at.desc())
    return list(session.exec(stmt).all())


def get(session: Session, project_id: uuid.UUID, kit_id: uuid.UUID) -> BrandKit:
    kit = session.get(BrandKit, kit_id)
    if kit is None or kit.project_id != project_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="brand_kit not found")
    return kit


def update(session: Session, project_id: uuid.UUID, kit_id: uuid.UUID, payload: BrandKitUpdate) -> BrandKit:
    kit = get(session, project_id, kit_id)
    data = payload.model_dump(exclude_unset=True)
    for key, value in data.items():
        setattr(kit, key, value)
    session.add(kit)
    session.flush()
    if data.get("is_default"):
        _unset_defaults_for_project(session, project_id, except_id=kit.id)
    session.refresh(kit)
    return kit


def delete(session: Session, project_id: uuid.UUID, kit_id: uuid.UUID) -> None:
    kit = get(session, project_id, kit_id)
    session.delete(kit)
    session.flush()
