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
from app.schemas.render_variants import RenderVariantRead
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


@stock_router.get("/stock", response_model=List[RenderVariantRead])
def stock(
    preset: Optional[str] = Query(default=None),
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> List[RenderVariantRead]:
    """Approved render_variants not yet pinned to an active plan slot."""
    if preset:
        rows = planner.stock_for_preset(session, project.id, preset, limit=200)
    else:
        rows = planner.stock_for_project(session, project.id)
    return [RenderVariantRead.model_validate(r) for r in rows]


@stock_router.get("/calendar", response_model=List[PlanSlotRead])
def calendar(
    from_: datetime = Query(..., alias="from"),
    to: datetime = Query(...),
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> List[PlanSlotRead]:
    """Slots in [from, to). Used by the admin's calendar view."""
    from app.models.plan_slots import PlanSlot
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
    return [PlanSlotRead.model_validate(s) for s in session.exec(stmt).all()]
