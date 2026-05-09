"""Cost reporting — aggregations over `generation_calls`.

Used by the admin dashboard:
- per-project cost summary across an arbitrary time window
- per-scenario generation_calls list (drill-down)
- weekly budget remaining (PLAN § 8)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

from sqlalchemy import func
from sqlmodel import Session, select

from app.models.generation_calls import GenerationCall
from app.models.projects import Project
from app.schemas.cost import CostSummary, TaskBreakdown


def _coerce_period(from_: Optional[datetime], to: Optional[datetime]) -> Tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    period_to = to or now
    period_from = from_ or (period_to - timedelta(days=30))
    return period_from, period_to


def list_calls_for_scenario(
    session: Session,
    scenario_id: uuid.UUID,
    *,
    limit: int = 200,
    offset: int = 0,
) -> List[GenerationCall]:
    stmt = (
        select(GenerationCall)
        .where(GenerationCall.scenario_id == scenario_id)
        .order_by(GenerationCall.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(session.exec(stmt).all())


def project_summary(
    session: Session,
    project: Project,
    from_: Optional[datetime] = None,
    to: Optional[datetime] = None,
) -> CostSummary:
    period_from, period_to = _coerce_period(from_, to)

    base = select(GenerationCall).where(
        GenerationCall.project_id == project.id,
        GenerationCall.created_at >= period_from,
        GenerationCall.created_at <= period_to,
    )

    # Totals
    total_stmt = select(
        func.coalesce(func.sum(GenerationCall.cost_usd), 0),
        func.count(GenerationCall.id),
        func.count().filter(GenerationCall.status == "success"),
        func.count().filter(GenerationCall.status != "success"),
    ).where(
        GenerationCall.project_id == project.id,
        GenerationCall.created_at >= period_from,
        GenerationCall.created_at <= period_to,
    )
    total_cost, total_calls, success_calls, failed_calls = session.exec(total_stmt).one()

    # By task
    by_task_stmt = (
        select(
            GenerationCall.task_key,
            func.count(GenerationCall.id),
            func.count().filter(GenerationCall.status == "success"),
            func.count().filter(GenerationCall.status != "success"),
            func.coalesce(func.sum(GenerationCall.cost_usd), 0),
        )
        .where(
            GenerationCall.project_id == project.id,
            GenerationCall.created_at >= period_from,
            GenerationCall.created_at <= period_to,
        )
        .group_by(GenerationCall.task_key)
    )
    by_task = [
        TaskBreakdown(task_key=row[0], call_count=row[1], success_count=row[2], failed_count=row[3], cost_usd=float(row[4]))
        for row in session.exec(by_task_stmt).all()
    ]

    # By provider (kept as a loose dict — admins may add new provider names anytime)
    by_provider_stmt = (
        select(
            GenerationCall.provider,
            GenerationCall.model_id,
            func.count(GenerationCall.id),
            func.coalesce(func.sum(GenerationCall.cost_usd), 0),
        )
        .where(
            GenerationCall.project_id == project.id,
            GenerationCall.created_at >= period_from,
            GenerationCall.created_at <= period_to,
        )
        .group_by(GenerationCall.provider, GenerationCall.model_id)
        .order_by(func.coalesce(func.sum(GenerationCall.cost_usd), 0).desc())
    )
    by_provider = [
        {"provider": row[0], "model_id": row[1], "call_count": row[2], "cost_usd": float(row[3])}
        for row in session.exec(by_provider_stmt).all()
    ]

    # Weekly budget remaining
    weekly_remaining = None
    if project.weekly_budget_cap_usd is not None:
        # Spend since the start of the current ISO week (Monday 00:00 UTC).
        now = datetime.now(timezone.utc)
        week_start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        spent_stmt = select(func.coalesce(func.sum(GenerationCall.cost_usd), 0)).where(
            GenerationCall.project_id == project.id,
            GenerationCall.created_at >= week_start,
        )
        spent = float(session.exec(spent_stmt).one() or 0.0)
        weekly_remaining = float(project.weekly_budget_cap_usd) - spent

    return CostSummary(
        project_id=project.id,
        period_from=period_from,
        period_to=period_to,
        total_cost_usd=float(total_cost or 0),
        total_calls=int(total_calls or 0),
        success_calls=int(success_calls or 0),
        failed_calls=int(failed_calls or 0),
        by_task=by_task,
        by_provider=by_provider,
        weekly_budget_cap_usd=float(project.weekly_budget_cap_usd) if project.weekly_budget_cap_usd else None,
        weekly_budget_remaining_usd=weekly_remaining,
    )
