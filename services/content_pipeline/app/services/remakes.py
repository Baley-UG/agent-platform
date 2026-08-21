"""Remake orchestration service — create, plan edits, the two gates.

The reconciler (`remake_reconciler.advance`) owns execution; this module
owns the human-facing transitions and the DB shape around them. Cost
estimates are computed here in code (never guessed by the LLM) from the
step graph × `model_routes` prices.
"""

from __future__ import annotations

import uuid
from typing import List, Optional

from fastapi import HTTPException, status
from sqlmodel import Session, select

from app.core.logging import logger
from app.models._base import utcnow
from app.models.content_references import ContentReference
from app.models.projects import Project
from app.models.remake_shots import RemakeShot
from app.models.remake_steps import RemakeStep
from app.models.remakes import Remake
from app.schemas.remakes import PlanPatch, RemakeCreate
from app.services import presets as presets_svc
from app.services import remake_reconciler
from app.services import remake_steps_author as author
from app.services import remake_cost as cost_svc

# In Phase 1 the generative techniques (restyle/reframe) are not wired;
# the planner's suggestions are clamped to erase at approval time.
PHASE1_ONLY = True


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


def create(
    session: Session,
    project: Project,
    payload: RemakeCreate,
    *,
    created_by: Optional[str] = None,
) -> Remake:
    """Spawn a remake and enqueue the analysis phase."""
    reference = session.get(ContentReference, payload.reference_id)
    if reference is None or reference.project_id != project.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="reference not found")
    if not reference.media_s3_key:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "reference has no mirrored source video; "
                f"POST /references/{reference.id}/remirror first"
            ),
        )

    preset_key = payload.preset_key or presets_svc.recommend_preset_for_reference(reference)
    if preset_key not in presets_svc.PRESETS:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"unknown preset_key: {preset_key}")

    remake = Remake(
        project_id=project.id,
        reference_id=reference.id,
        brand_kit_id=payload.brand_kit_id,
        preset_key=preset_key,
        status="analyzing",
        source_s3_key=reference.media_s3_key,
        default_caption=(reference.caption or "").strip() or None,
        default_hashtags=list(reference.hashtags or []) or None,
        created_by=created_by,
    )
    session.add(remake)
    session.flush()
    session.refresh(remake)

    for step in author.build_analysis_steps(remake.id):
        session.add(step)
    session.flush()

    # Kick the analysis phase (soft-fails if Redis is down; the sweep
    # will pick it up).
    try:
        remake_reconciler.advance(session, remake.id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("remake_initial_advance_failed", remake_id=str(remake.id), error=str(exc))

    session.refresh(remake)
    return remake


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------


def get(session: Session, project_id: uuid.UUID, remake_id: uuid.UUID) -> Remake:
    row = session.get(Remake, remake_id)
    if row is None or row.project_id != project_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="remake not found")
    return row


def list_(
    session: Session, project_id: uuid.UUID, *, status_: Optional[str] = None, limit: int = 50, offset: int = 0
) -> List[Remake]:
    stmt = select(Remake).where(Remake.project_id == project_id)
    if status_:
        stmt = stmt.where(Remake.status == status_)
    stmt = stmt.order_by(Remake.created_at.desc()).limit(limit).offset(offset)
    return list(session.exec(stmt).all())


def shots_for(session: Session, remake_id: uuid.UUID) -> List[RemakeShot]:
    return list(
        session.exec(
            select(RemakeShot).where(RemakeShot.remake_id == remake_id).order_by(RemakeShot.idx)
        ).all()
    )


def steps_for(session: Session, remake_id: uuid.UUID) -> List[RemakeStep]:
    return list(
        session.exec(
            select(RemakeStep).where(RemakeStep.remake_id == remake_id).order_by(RemakeStep.seq)
        ).all()
    )


def progress(shots: List[RemakeShot]) -> dict:
    active = [s for s in shots if s.technique != "drop"]
    ready = [s for s in active if s.status == "ready"]
    return {"shots_total": len(active), "shots_ready": len(ready)}


# ---------------------------------------------------------------------------
# plan editing (Gate 1)
# ---------------------------------------------------------------------------


def patch_plan(session: Session, remake: Remake, patch: PlanPatch) -> Remake:
    if remake.status != "plan_review":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"plan is only editable in plan_review (status={remake.status})",
        )

    if patch.shots:
        by_idx = {s.idx: s for s in shots_for(session, remake.id)}
        for sp in patch.shots:
            shot = by_idx.get(sp.idx)
            if shot is None:
                continue
            if sp.technique is not None:
                shot.technique = sp.technique
            if sp.prompt is not None:
                shot.prompt = sp.prompt
            if sp.trim_start_sec is not None:
                shot.trim_start_sec = sp.trim_start_sec
            if sp.trim_end_sec is not None:
                shot.trim_end_sec = sp.trim_end_sec
            if sp.text_plan is not None:
                shot.text_plan = sp.text_plan
            session.add(shot)

    plan = dict(remake.plan_json or {})
    for field in ("audio_mode", "voice_script", "cta_text", "logo_overlay"):
        val = getattr(patch, field)
        if val is not None:
            plan[field] = val
    if patch.outro_template_id is not None:
        plan["outro_template_id"] = str(patch.outro_template_id)
    remake.plan_json = plan

    if patch.default_caption is not None:
        remake.default_caption = patch.default_caption
    if patch.default_hashtags is not None:
        remake.default_hashtags = patch.default_hashtags

    session.add(remake)
    session.flush()

    # Re-estimate now that techniques may have changed.
    cost_svc.estimate_remake(session, remake)
    session.refresh(remake)
    return remake


def approve_plan(session: Session, remake: Remake, *, approved_by: Optional[str] = None) -> Remake:
    """Gate 1 → author render steps, move to rendering, kick execution."""
    if remake.status != "plan_review":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"cannot approve plan from status={remake.status}",
        )
    shots = shots_for(session, remake.id)
    if not shots:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="no shots to render")

    if PHASE1_ONLY:
        for shot in shots:
            author.clamp_shot_for_phase1(shot)
            session.add(shot)

    for step in author.build_render_steps(remake, shots, phase1_only=PHASE1_ONLY):
        session.add(step)

    remake.status = "rendering"
    remake.plan_approved_at = utcnow()
    remake.plan_approved_by = approved_by
    session.add(remake)
    session.flush()

    remake_reconciler.advance(session, remake.id)
    session.refresh(remake)
    return remake


# ---------------------------------------------------------------------------
# retry / reject / final (Gate 2)
# ---------------------------------------------------------------------------


def retry_shot(session: Session, remake: Remake, shot_id: uuid.UUID) -> Remake:
    """needs_attention → reset that shot's failed steps and re-drive."""
    shot = session.get(RemakeShot, shot_id)
    if shot is None or shot.remake_id != remake.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="shot not found")
    steps = session.exec(select(RemakeStep).where(RemakeStep.shot_id == shot_id)).all()
    for s in steps:
        if s.status == "failed":
            s.status = "pending"
            s.attempts = 0
            s.error = None
            s.lease_expires_at = None
            session.add(s)
    shot.status = "rendering"
    shot.error = None
    session.add(shot)
    # A retry means the remake is working again.
    if remake.status == "needs_attention":
        remake.status = "rendering"
        session.add(remake)
    session.flush()
    remake_reconciler.advance(session, remake.id)
    session.refresh(remake)
    return remake


def reject_shot(
    session: Session,
    remake: Remake,
    shot_id: uuid.UUID,
    *,
    prompt_override: Optional[str] = None,
    technique: Optional[str] = None,
) -> Remake:
    """final_review → re-run one shot + recompose, back to rendering."""
    if remake.status != "final_review":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"shots can only be rejected in final_review (status={remake.status})",
        )
    shot = session.get(RemakeShot, shot_id)
    if shot is None or shot.remake_id != remake.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="shot not found")

    if technique:
        shot.technique = technique
    if prompt_override is not None:
        shot.prompt = prompt_override
    shot.output_s3_key = None
    shot.status = "planned"
    session.add(shot)

    # Drop this shot's old steps and re-author them for the (possibly
    # new) technique.
    for s in session.exec(select(RemakeStep).where(RemakeStep.shot_id == shot_id)).all():
        session.delete(s)
    if PHASE1_ONLY:
        author.clamp_shot_for_phase1(shot)
    for st in author._shot_steps(remake.id, shot):
        session.add(st)

    # Reset compose so it re-runs once the shot is ready again.
    compose = session.exec(
        select(RemakeStep).where(RemakeStep.remake_id == remake.id, RemakeStep.kind == "compose")
    ).first()
    if compose is not None:
        compose.status = "pending"
        compose.attempts = 0
        compose.error = None
        compose.lease_expires_at = None
        session.add(compose)

    remake.status = "rendering"
    remake.final_s3_key = None
    session.add(remake)
    session.flush()
    remake_reconciler.advance(session, remake.id)
    session.refresh(remake)
    return remake


def approve_final(
    session: Session,
    remake: Remake,
    *,
    approved_by: Optional[str] = None,
    plan_slot_id: Optional[uuid.UUID] = None,
) -> Remake:
    """Gate 2 → stamp the final media_asset, mark done, optional plan handoff."""
    if remake.status != "final_review":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"cannot approve final from status={remake.status}",
        )
    if not remake.final_s3_key:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="remake has no composed output")

    from app.services import media_assets as media_svc

    asset = media_svc.create_initial(
        session,
        project_id=remake.project_id,
        type_="final_video",
        s3_key=remake.final_s3_key,
        mime_type="video/mp4",
        parent_remake_id=remake.id,
        metadata={"reference_id": str(remake.reference_id), "preset_key": remake.preset_key},
    )
    remake.final_media_asset_id = asset.id
    remake.status = "done"
    remake.final_approved_at = utcnow()
    remake.final_approved_by = approved_by
    session.add(remake)
    session.flush()

    if plan_slot_id is not None:
        from app.models.plan_slots import PlanSlot

        slot = session.get(PlanSlot, plan_slot_id)
        if slot is not None and slot.project_id == remake.project_id:
            slot.variant_id = remake.id  # `variant_id` points at the remake
            slot.source_kind = "stock"
            slot.status = "ready"
            session.add(slot)
            session.flush()

    session.refresh(remake)
    return remake


def archive(session: Session, remake: Remake) -> Remake:
    remake.status = "archived"
    session.add(remake)
    session.flush()
    session.refresh(remake)
    return remake
