"""Budget enforcement helpers — read `generation_calls`, decide if more spend is OK.

Used by:
- the auto-generation loop (don't enqueue if a project is over its weekly cap)
- API endpoints that want to surface remaining budget alongside an action
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func
from sqlmodel import Session, select

from app.models.generation_calls import GenerationCall
from app.models.projects import Project


def week_start_utc(now: Optional[datetime] = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)


def day_start_utc(now: Optional[datetime] = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def spent_in_window(session: Session, project_id: uuid.UUID, since: datetime) -> float:
    stmt = select(func.coalesce(func.sum(GenerationCall.cost_usd), 0)).where(
        GenerationCall.project_id == project_id,
        GenerationCall.created_at >= since,
    )
    return float(session.exec(stmt).one() or 0.0)


def weekly_spent(session: Session, project_id: uuid.UUID) -> float:
    return spent_in_window(session, project_id, week_start_utc())


def daily_spent(session: Session, project_id: uuid.UUID) -> float:
    return spent_in_window(session, project_id, day_start_utc())


def has_weekly_budget_remaining(session: Session, project: Project, *, headroom_usd: float = 0.0) -> bool:
    """True when the project's weekly spend leaves at least `headroom_usd` of cap free.

    `headroom_usd` is the worst-case cost of the next operation; pass a
    representative value (e.g. 0.50 for a typical scenario) so we don't
    enqueue a job that's certain to bust the cap.
    """
    cap = project.weekly_budget_cap_usd
    if cap is None:
        return True
    spent = weekly_spent(session, project.id)
    return float(cap) - spent >= headroom_usd


def has_rule_budget_remaining(
    session: Session, project_id: uuid.UUID, rule_cap_usd: Optional[float], *, headroom_usd: float = 0.0
) -> bool:
    """A per-rule cap (auto_generation_rules.budget_cap_usd) measured weekly."""
    if rule_cap_usd is None:
        return True
    spent = weekly_spent(session, project_id)
    return float(rule_cap_usd) - spent >= headroom_usd
