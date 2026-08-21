"""weekly_plans + plan_slots + stock + calendar endpoints."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, Query, status
from sqlmodel import Session

from app.api.v1.deps import get_project, get_session, require_api_key
from app.models.projects import Project
from app.schemas.plans import (
    AssignVariantRequest,
    PlanSlotCreate,
    PlanSlotRead,
    PlanSlotUpdate,
    WeeklyPlanGenerateRequest,
    WeeklyPlanRead,
)
from app.schemas.remakes import RemakeRead
from app.services import planner
from app.services import weekly_plans as svc

# ---- weekly plans (project-scoped) ----
plan_router = APIRouter(
    prefix="/projects/{project_id}/weekly-plans",
    tags=["weekly-plans"],
    dependencies=[Depends(require_api_key)],
)


@plan_router.post("/generate", response_model=WeeklyPlanRead, status_code=status.HTTP_201_CREATED)
def generate_plan(
    payload: WeeklyPlanGenerateRequest,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> WeeklyPlanRead:
    plan = svc.generate(session, project, payload.week_start, fill=payload.fill, generated_by="api")
    return WeeklyPlanRead.model_validate(plan)


@plan_router.get("", response_model=List[WeeklyPlanRead])
def list_plans(
    project: Project = Depends(get_project), session: Session = Depends(get_session)
) -> List[WeeklyPlanRead]:
    return [WeeklyPlanRead.model_validate(p) for p in svc.list_(session, project.id)]


@plan_router.get("/{plan_id}", response_model=WeeklyPlanRead)
def get_plan(
    plan_id: uuid.UUID, project: Project = Depends(get_project), session: Session = Depends(get_session)
) -> WeeklyPlanRead:
    return WeeklyPlanRead.model_validate(svc.get(session, project.id, plan_id))


@plan_router.get("/{plan_id}/slots", response_model=List[PlanSlotRead])
def list_slots(
    plan_id: uuid.UUID, project: Project = Depends(get_project), session: Session = Depends(get_session)
) -> List[PlanSlotRead]:
    svc.get(session, project.id, plan_id)
    return [PlanSlotRead.model_validate(s) for s in svc.list_slots_for_plan(session, plan_id)]


@plan_router.post("/{plan_id}/approve", response_model=WeeklyPlanRead)
def approve_plan(
    plan_id: uuid.UUID, project: Project = Depends(get_project), session: Session = Depends(get_session)
) -> WeeklyPlanRead:
    plan = svc.get(session, project.id, plan_id)
    return WeeklyPlanRead.model_validate(svc.approve(session, plan))


@plan_router.post("/{plan_id}/refill", response_model=WeeklyPlanRead)
def refill_plan(
    plan_id: uuid.UUID, project: Project = Depends(get_project), session: Session = Depends(get_session)
) -> WeeklyPlanRead:
    """Re-run the fill_strategy over all empty slots (e.g. after new stock arrived)."""
    plan = svc.get(session, project.id, plan_id)
    svc.fill_empty_slots(session, plan)
    return WeeklyPlanRead.model_validate(plan)


# ---- plan_slots (top-level for drag-drop convenience) ----
slot_router = APIRouter(
    prefix="/projects/{project_id}/plan-slots",
    tags=["plan-slots"],
    dependencies=[Depends(require_api_key)],
)


@slot_router.post("", response_model=PlanSlotRead, status_code=status.HTTP_201_CREATED)
def create_slot(
    payload: PlanSlotCreate,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> PlanSlotRead:
    plan = svc.get(session, project.id, payload.weekly_plan_id)
    from app.models.plan_slots import PlanSlot

    slot = PlanSlot(
        weekly_plan_id=plan.id,
        project_id=project.id,
        scheduled_at=payload.scheduled_at,
        social_account_id=payload.social_account_id or project.default_social_account_id,
        content_type=payload.content_type,
        variant_preset=payload.variant_preset,
        variant_id=payload.variant_id,
        source_kind="stock" if payload.variant_id else "manual",
        status="ready" if payload.variant_id else "empty",
    )
    session.add(slot)
    session.flush()
    session.refresh(slot)
    return PlanSlotRead.model_validate(slot)


@slot_router.patch("/{slot_id}", response_model=PlanSlotRead)
def update_slot(
    slot_id: uuid.UUID,
    payload: PlanSlotUpdate,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> PlanSlotRead:
    slot = svc.get_slot(session, project.id, slot_id)
    return PlanSlotRead.model_validate(svc.update_slot(session, slot, payload.model_dump(exclude_unset=True)))


@slot_router.post("/{slot_id}/assign-variant", response_model=PlanSlotRead)
def assign_variant(
    slot_id: uuid.UUID,
    payload: AssignVariantRequest,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> PlanSlotRead:
    slot = svc.get_slot(session, project.id, slot_id)
    return PlanSlotRead.model_validate(svc.assign_variant(session, slot, payload.variant_id))


@slot_router.post("/{slot_id}/skip", response_model=PlanSlotRead)
def skip_slot(
    slot_id: uuid.UUID, project: Project = Depends(get_project), session: Session = Depends(get_session)
) -> PlanSlotRead:
    slot = svc.get_slot(session, project.id, slot_id)
    return PlanSlotRead.model_validate(svc.skip_slot(session, slot))


@slot_router.delete("/{slot_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_slot(
    slot_id: uuid.UUID, project: Project = Depends(get_project), session: Session = Depends(get_session)
) -> None:
    slot = svc.get_slot(session, project.id, slot_id)
    svc.delete_slot(session, slot)


# ---- stock + calendar ----
stock_router = APIRouter(
    prefix="/projects/{project_id}",
    tags=["plans"],
    dependencies=[Depends(require_api_key)],
)


@stock_router.get("/stock", response_model=List[RemakeRead])
def stock(
    preset: Optional[str] = Query(default=None),
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> List[RemakeRead]:
    """Done remakes not yet pinned to an active plan slot."""
    if preset:
        rows = planner.stock_for_preset(session, project.id, preset, limit=200)
    else:
        rows = planner.stock_for_project(session, project.id)
    return [RemakeRead.model_validate(r) for r in rows]


@stock_router.get("/calendar")
def calendar(
    from_: datetime = Query(..., alias="from"),
    to: datetime = Query(...),
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> List[dict]:
    """Slots in [from, to). Returns each slot enriched with the resolved
    caption (slot override → remake default → reference caption) and
    the upstream remake/reference ids so the calendar can render a
    content snapshot without firing one HTTP call per event.

    Plain PlanSlotRead fields are preserved so existing callers keep
    working; the new fields are additive (`caption_resolved`,
    `remake_id`, `reference_id`).
    """
    from app.models.content_references import ContentReference
    from app.models.plan_slots import PlanSlot
    from app.models.remakes import Remake
    from sqlmodel import select

    stmt = (
        select(PlanSlot)
        .where(
            PlanSlot.project_id == project.id,
            PlanSlot.scheduled_at >= from_,
            PlanSlot.scheduled_at < to,
        )
        .order_by(PlanSlot.scheduled_at.asc())
    )
    slots = list(session.exec(stmt).all())

    # Batch-load remakes + references so we don't N+1. `variant_id`
    # points at a remake id.
    remake_ids = {s.variant_id for s in slots if s.variant_id}
    remakes_by_id: dict = {}
    references_by_id: dict = {}
    if remake_ids:
        rms = session.exec(select(Remake).where(Remake.id.in_(remake_ids))).all()
        remakes_by_id = {r.id: r for r in rms}
        ref_ids = {r.reference_id for r in rms if r.reference_id}
        if ref_ids:
            refs = session.exec(
                select(ContentReference).where(ContentReference.id.in_(ref_ids))
            ).all()
            references_by_id = {r.id: r for r in refs}

    out: List[dict] = []
    for slot in slots:
        base = PlanSlotRead.model_validate(slot).model_dump(mode="json")
        remake = remakes_by_id.get(slot.variant_id) if slot.variant_id else None
        reference = (
            references_by_id.get(remake.reference_id) if remake and remake.reference_id else None
        )

        caption = (
            slot.caption_override
            or (remake.default_caption if remake else None)
            or (reference.caption if reference else None)
        )
        hashtags = (
            list(slot.hashtags_override or [])
            or list((remake.default_hashtags if remake else None) or [])
            or list((reference.hashtags if reference else None) or [])
        )

        base["caption_resolved"] = caption
        base["hashtags_resolved"] = hashtags or None
        base["remake_id"] = str(remake.id) if remake else None
        base["reference_id"] = str(remake.reference_id) if remake and remake.reference_id else base.get("reference_id")
        out.append(base)

    return out
