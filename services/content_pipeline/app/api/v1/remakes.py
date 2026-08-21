"""Remake endpoints — the competitor-ad remake vertical (CP-M10).

Two human gates: `approve-plan` (Gate 1) and `approve-final` (Gate 2).
Everything between runs automatically via the reconciler.
"""

from __future__ import annotations

import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, Query, status
from sqlmodel import Session

from app.api.v1.deps import get_project, get_session, require_api_key
from app.models.projects import Project
from app.schemas.remakes import (
    ApproveFinalRequest,
    PlanPatch,
    RemakeCreate,
    RemakeDetail,
    RemakeRead,
    ShotRead,
    ShotRejectRequest,
    StepRead,
)
from app.services import remakes as svc

router = APIRouter(
    prefix="/projects/{project_id}/remakes",
    tags=["remakes"],
    dependencies=[Depends(require_api_key)],
)


def _detail(session: Session, remake) -> RemakeDetail:
    shots = svc.shots_for(session, remake.id)
    steps = svc.steps_for(session, remake.id)
    payload = RemakeDetail.model_validate(remake, from_attributes=True)
    payload.shots = [ShotRead.model_validate(s, from_attributes=True) for s in shots]
    payload.steps = [StepRead.model_validate(s, from_attributes=True) for s in steps]
    payload.progress = svc.progress(shots)
    # Presign the composed video so the review page can play it inline
    # against the private bucket.
    if remake.final_s3_key:
        from app.core import s3 as s3lib

        try:
            payload.final_url = s3lib.presigned_get_url(remake.final_s3_key, ttl=3600)
        except Exception:  # noqa: BLE001
            payload.final_url = None
    return payload


@router.post("", response_model=RemakeRead, status_code=status.HTTP_201_CREATED)
def create(
    payload: RemakeCreate,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> RemakeRead:
    remake = svc.create(session, project, payload, created_by="api")
    return RemakeRead.model_validate(remake, from_attributes=True)


@router.get("", response_model=List[RemakeRead])
def list_(
    status_: Optional[str] = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> List[RemakeRead]:
    rows = svc.list_(session, project.id, status_=status_, limit=limit, offset=offset)
    return [RemakeRead.model_validate(r, from_attributes=True) for r in rows]


@router.get("/{remake_id}", response_model=RemakeDetail)
def get(
    remake_id: uuid.UUID,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> RemakeDetail:
    remake = svc.get(session, project.id, remake_id)
    return _detail(session, remake)


@router.patch("/{remake_id}/plan", response_model=RemakeDetail)
def patch_plan(
    remake_id: uuid.UUID,
    payload: PlanPatch,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> RemakeDetail:
    remake = svc.get(session, project.id, remake_id)
    svc.patch_plan(session, remake, payload)
    return _detail(session, remake)


@router.post("/{remake_id}/approve-plan", response_model=RemakeDetail)
def approve_plan(
    remake_id: uuid.UUID,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> RemakeDetail:
    remake = svc.get(session, project.id, remake_id)
    svc.approve_plan(session, remake, approved_by="api")
    return _detail(session, remake)


@router.post("/{remake_id}/shots/{shot_id}/retry", response_model=RemakeDetail)
def retry_shot(
    remake_id: uuid.UUID,
    shot_id: uuid.UUID,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> RemakeDetail:
    remake = svc.get(session, project.id, remake_id)
    svc.retry_shot(session, remake, shot_id)
    return _detail(session, remake)


@router.post("/{remake_id}/shots/{shot_id}/reject", response_model=RemakeDetail)
def reject_shot(
    remake_id: uuid.UUID,
    shot_id: uuid.UUID,
    payload: ShotRejectRequest,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> RemakeDetail:
    remake = svc.get(session, project.id, remake_id)
    svc.reject_shot(
        session, remake, shot_id,
        prompt_override=payload.prompt_override, technique=payload.technique,
    )
    return _detail(session, remake)


@router.post("/{remake_id}/approve-final", response_model=RemakeRead)
def approve_final(
    remake_id: uuid.UUID,
    payload: ApproveFinalRequest,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> RemakeRead:
    remake = svc.get(session, project.id, remake_id)
    svc.approve_final(session, remake, approved_by="api", plan_slot_id=payload.plan_slot_id)
    return RemakeRead.model_validate(remake, from_attributes=True)


@router.post("/{remake_id}/archive", response_model=RemakeRead)
def archive(
    remake_id: uuid.UUID,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> RemakeRead:
    remake = svc.get(session, project.id, remake_id)
    svc.archive(session, remake)
    return RemakeRead.model_validate(remake, from_attributes=True)
