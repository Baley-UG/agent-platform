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
    RejectFinalRequest,
    PlanPatch,
    RemakeCreate,
    RemakeDetail,
    RemakeImportExternal,
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


def _enrich(session: Session, payloads, remakes) -> None:
    """Stamp poster_url + reference_title from the source references —
    one bulk query per page, presigns only when a poster exists."""
    from app.core import s3 as s3lib
    from app.models.content_references import ContentReference
    from sqlmodel import select as _select

    ref_ids = {r.reference_id for r in remakes if r.reference_id}
    if not ref_ids:
        return
    refs = {
        r.id: r
        for r in session.exec(
            _select(ContentReference).where(ContentReference.id.in_(ref_ids))
        ).all()
    }
    for payload, remake in zip(payloads, remakes):
        ref = refs.get(remake.reference_id)
        if ref is None:
            continue
        payload.reference_title = ref.title or (ref.caption or "")[:80] or None
        payload.reference_tags = list(ref.tags) if ref.tags else None
        key = ref.poster_s3_key or (
            ref.media_s3_key
            if (ref.media_s3_key or "").lower().endswith((".jpg", ".jpeg", ".png", ".webp"))
            else None
        )
        if key:
            try:
                payload.poster_url = s3lib.presigned_get_url(key, ttl=3600)
            except Exception:  # noqa: BLE001
                payload.poster_url = None


def _detail(session: Session, remake) -> RemakeDetail:
    shots = svc.shots_for(session, remake.id)
    steps = svc.steps_for(session, remake.id)
    payload = RemakeDetail.model_validate(remake, from_attributes=True)
    payload.shots = [ShotRead.model_validate(s, from_attributes=True) for s in shots]
    payload.steps = [StepRead.model_validate(s, from_attributes=True) for s in steps]
    payload.progress = svc.progress(shots)
    _enrich(session, [payload], [remake])
    # Presign the composed video so the review page can play it inline
    # against the private bucket.
    from app.core import s3 as s3lib

    if remake.final_s3_key:
        try:
            payload.final_url = s3lib.presigned_get_url(remake.final_s3_key, ttl=3600)
        except Exception:  # noqa: BLE001
            payload.final_url = None
    # The source reference video, for the side-by-side compare on the
    # review pages. Skip image mirrors (image-only references) — a JPEG
    # in a <video> tag never loads and would wedge the synced player.
    if remake.source_s3_key and not remake.source_s3_key.lower().endswith(
        (".jpg", ".jpeg", ".png", ".webp", ".gif")
    ):
        try:
            payload.source_url = s3lib.presigned_get_url(remake.source_s3_key, ttl=3600)
        except Exception:  # noqa: BLE001
            payload.source_url = None
    # Server-generated timeline frames. Generated lazily by the ffmpeg
    # worker on first view of a finished remake; until then the panel
    # captures frames client-side.
    fs = (remake.plan_json or {}).get("filmstrip")
    if isinstance(fs, dict) and fs.get("final"):
        try:
            payload.filmstrip = {
                kind: [s3lib.presigned_get_url(k, ttl=3600) for k in keys]
                for kind, keys in fs.items()
                if isinstance(keys, list) and keys
            }
        except Exception:  # noqa: BLE001
            payload.filmstrip = None
    elif remake.final_s3_key and remake.status in ("final_review", "done"):
        from app.services.queue import enqueue

        try:
            enqueue(
                "remake_ffmpeg",
                "app.workers.remake_filmstrip.run",
                str(remake.id),
                job_id=f"rmfilmstrip_{remake.id}",
            )
        except Exception:  # noqa: BLE001 — best-effort; the panel has a fallback
            pass
    return payload


@router.post("", response_model=RemakeRead, status_code=status.HTTP_201_CREATED)
def create(
    payload: RemakeCreate,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> RemakeRead:
    remake = svc.create(session, project, payload, created_by="api")
    return RemakeRead.model_validate(remake, from_attributes=True)


@router.post("/import-external", response_model=RemakeRead, status_code=status.HTTP_201_CREATED)
def import_external(
    payload: RemakeImportExternal,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> RemakeRead:
    """Register an externally-produced video (fetched from `video_url`)
    as a remake in `final_review` — the normal Gate-2 approve → done →
    stock/publish flow applies from there."""
    remake = svc.import_external(
        session, project,
        reference_id=payload.reference_id,
        video_url=payload.video_url,
        s3_key=payload.s3_key,
        caption=payload.caption,
        cost_usd=payload.cost_usd,
        created_by="api",
    )
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
    payloads = [RemakeRead.model_validate(r, from_attributes=True) for r in rows]
    _enrich(session, payloads, rows)
    return payloads


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


@router.post("/{remake_id}/retry", response_model=RemakeDetail)
def retry(
    remake_id: uuid.UUID,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> RemakeDetail:
    """Recover a needs_attention remake by re-driving every failed step
    (including global ones like scene_detect / author_plan / compose that
    the per-shot retry can't reach)."""
    remake = svc.get(session, project.id, remake_id)
    svc.retry(session, remake)
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


@router.post("/{remake_id}/reject-final", response_model=RemakeRead)
def reject_final(
    remake_id: uuid.UUID,
    payload: Optional[RejectFinalRequest] = None,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> RemakeRead:
    """Gate-2 rejection: final_review → rejected (terminal, distinct
    from archived so it can be filtered/audited). Optional {reason}."""
    remake = svc.get(session, project.id, remake_id)
    svc.reject_final(session, remake, reason=payload.reason if payload else None)
    return RemakeRead.model_validate(remake, from_attributes=True)


@router.post("/{remake_id}/reopen", response_model=RemakeDetail)
def reopen(
    remake_id: uuid.UUID,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> RemakeDetail:
    """rejected → final_review: bring a rejected remake back to Gate 2."""
    remake = svc.get(session, project.id, remake_id)
    svc.reopen_final(session, remake)
    return _detail(session, remake)


@router.post("/{remake_id}/unapprove-final", response_model=RemakeDetail)
def unapprove_final(
    remake_id: uuid.UUID,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> RemakeDetail:
    """Revert a mistaken final approval: done → final_review. Refused
    when the remake was already published from a plan slot."""
    remake = svc.get(session, project.id, remake_id)
    svc.unapprove_final(session, remake)
    return _detail(session, remake)


@router.post("/{remake_id}/archive", response_model=RemakeRead)
def archive(
    remake_id: uuid.UUID,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> RemakeRead:
    remake = svc.get(session, project.id, remake_id)
    svc.archive(session, remake)
    return RemakeRead.model_validate(remake, from_attributes=True)
