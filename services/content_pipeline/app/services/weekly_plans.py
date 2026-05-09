"""weekly_plans + plan_slots CRUD + skeleton generation orchestrator."""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import List, Optional

from fastapi import HTTPException, status
from sqlmodel import Session, select

from app.models.plan_slots import PlanSlot
from app.models.projects import Project
from app.models.weekly_plans import WeeklyPlan
from app.services import planner
from app.services import posting_strategy as strategy_svc


# ----- weekly_plans -----


def list_(session: Session, project_id: uuid.UUID, *, limit: int = 26) -> List[WeeklyPlan]:
    stmt = (
        select(WeeklyPlan)
        .where(WeeklyPlan.project_id == project_id)
        .order_by(WeeklyPlan.week_start_date.desc())
        .limit(limit)
    )
    return list(session.exec(stmt).all())


def get(session: Session, project_id: uuid.UUID, plan_id: uuid.UUID) -> WeeklyPlan:
    row = session.get(WeeklyPlan, plan_id)
    if row is None or row.project_id != project_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="weekly_plan not found")
    return row


def get_for_week(session: Session, project_id: uuid.UUID, week_start: date) -> Optional[WeeklyPlan]:
    return session.exec(
        select(WeeklyPlan).where(
            WeeklyPlan.project_id == project_id,
            WeeklyPlan.week_start_date == week_start,
        )
    ).first()


def approve(session: Session, plan: WeeklyPlan) -> WeeklyPlan:
    plan.status = "approved"
    session.add(plan)
    session.flush()
    return plan


def archive(session: Session, plan: WeeklyPlan) -> WeeklyPlan:
    plan.status = "archived"
    session.add(plan)
    session.flush()
    return plan


# ----- skeleton generation -----


def generate(
    session: Session,
    project: Project,
    week_start: date,
    *,
    fill: bool = True,
    generated_by: Optional[str] = None,
) -> WeeklyPlan:
    """Materialize a weekly_plan + its slots for `week_start` (Monday).

    Idempotent: re-running for the same week reuses the existing plan and
    fills any missing slots. Slots already pinned to a variant stay put.
    """
    week_start = planner.monday_of(week_start)
    strategy = strategy_svc.get_or_create(session, project.id)

    plan = get_for_week(session, project.id, week_start)
    if plan is None:
        plan = WeeklyPlan(
            project_id=project.id,
            week_start_date=week_start,
            status="draft",
            generated_by=generated_by or "api",
        )
        session.add(plan)
        session.flush()

    expanded = planner.expand_preferred_slots(strategy, week_start)
    existing_keys = {
        (s.scheduled_at, s.variant_preset)
        for s in session.exec(select(PlanSlot).where(PlanSlot.weekly_plan_id == plan.id)).all()
    }

    inserted = 0
    for scheduled_at, preset, content_type in expanded:
        # Apply blackout (skip silently — admin can edit later if needed).
        try:
            from zoneinfo import ZoneInfo

            if planner.is_in_blackout(scheduled_at, strategy.blackout, ZoneInfo(strategy.timezone)):
                continue
        except Exception:  # noqa: BLE001
            pass

        if (scheduled_at, preset) in existing_keys:
            continue

        slot = PlanSlot(
            weekly_plan_id=plan.id,
            project_id=project.id,
            scheduled_at=scheduled_at,
            content_type=content_type,
            variant_preset=preset,
            social_account_id=project.default_social_account_id,
            source_kind="empty",
            status="empty",
        )
        session.add(slot)
        inserted += 1
    if inserted:
        session.flush()

    if fill:
        fill_empty_slots(session, plan, strategy)

    return plan


def fill_empty_slots(session: Session, plan: WeeklyPlan, strategy=None) -> int:
    """Apply the project's fill_strategy to all empty slots in this plan.

    Returns the number of slots changed.
    """
    if strategy is None:
        strategy = strategy_svc.get_or_create(session, plan.project_id)
    mode = strategy.fill_strategy
    empty_slots = list(
        session.exec(
            select(PlanSlot).where(PlanSlot.weekly_plan_id == plan.id, PlanSlot.status == "empty")
        ).all()
    )
    changed = 0
    for slot in empty_slots:
        if mode == "manual":
            continue
        if mode == "auto_suggest":
            ids = planner.suggest_for_slot(session, slot, k=3)
            if ids:
                slot.suggested_variant_ids = ids
                session.add(slot)
                changed += 1
        elif mode == "auto_fill":
            chosen = planner.auto_fill_slot(session, slot)
            if chosen is not None:
                changed += 1
    if changed:
        session.flush()
    return changed


# ----- plan_slots CRUD -----


def list_slots_for_plan(session: Session, plan_id: uuid.UUID) -> List[PlanSlot]:
    stmt = (
        select(PlanSlot)
        .where(PlanSlot.weekly_plan_id == plan_id)
        .order_by(PlanSlot.scheduled_at.asc())
    )
    return list(session.exec(stmt).all())


def get_slot(session: Session, project_id: uuid.UUID, slot_id: uuid.UUID) -> PlanSlot:
    row = session.get(PlanSlot, slot_id)
    if row is None or row.project_id != project_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="plan_slot not found")
    return row


def update_slot(session: Session, slot: PlanSlot, patch: dict) -> PlanSlot:
    """Drag-drop / variant assign / skip — admin-edit a slot."""
    allowed = {"scheduled_at", "social_account_id", "variant_id", "reference_id", "status", "content_type", "variant_preset"}
    for key, value in patch.items():
        if key in allowed and value is not None:
            setattr(slot, key, value)
    if patch.get("variant_id") is not None:
        slot.source_kind = "stock"
        slot.status = "ready" if slot.status in ("empty", "filling") else slot.status
    session.add(slot)
    session.flush()
    return slot


def assign_variant(session: Session, slot: PlanSlot, variant_id: uuid.UUID) -> PlanSlot:
    slot.variant_id = variant_id
    slot.source_kind = "stock"
    slot.status = "ready"
    session.add(slot)
    session.flush()
    return slot


def skip_slot(session: Session, slot: PlanSlot) -> PlanSlot:
    slot.status = "skipped"
    session.add(slot)
    session.flush()
    return slot


def delete_slot(session: Session, slot: PlanSlot) -> None:
    session.delete(slot)
    session.flush()


def due_slots(session: Session, *, now: Optional[None] = None) -> List[PlanSlot]:
    """Slots whose scheduled_at has passed and that are ready to publish.

    The publisher poller (scheduler) calls this every minute.
    """
    from datetime import datetime, timezone

    when = now or datetime.now(timezone.utc)
    stmt = (
        select(PlanSlot)
        .where(
            PlanSlot.scheduled_at <= when,
            PlanSlot.status == "ready",
            PlanSlot.variant_id.is_not(None),
            PlanSlot.social_account_id.is_not(None),
        )
        .order_by(PlanSlot.scheduled_at.asc())
    )
    return list(session.exec(stmt).all())
