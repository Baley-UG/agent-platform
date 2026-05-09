"""Central AI model router.

`resolve(project_id, task_key)` returns the lowest-priority enabled
`ModelRoute` row for that task, preferring project-scoped over global.
Multiple rows form a fallback chain — `resolve_chain()` returns all of
them sorted, and the caller (provider client) walks them on failure.

CRUD endpoints in `app/api/v1/model_routes.py` use `create_or_update` to
ensure the unique-per-priority constraint is honoured.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import HTTPException, status
from sqlmodel import Session, select

from app.models.model_routes import ModelRoute
from app.schemas.model_routes import ModelRouteCreate, ModelRouteUpdate


class NoRouteError(LookupError):
    """Raised when no enabled route exists for a task_key."""


def resolve_chain(session: Session, task_key: str, project_id: Optional[uuid.UUID] = None) -> List[ModelRoute]:
    """Return all enabled routes for a task, project-scoped first then global, sorted by priority."""
    rows: List[ModelRoute] = []

    if project_id is not None:
        stmt = (
            select(ModelRoute)
            .where(
                ModelRoute.project_id == project_id,
                ModelRoute.task_key == task_key,
                ModelRoute.enabled == True,  # noqa: E712
            )
            .order_by(ModelRoute.priority)
        )
        rows.extend(session.exec(stmt).all())

    stmt_global = (
        select(ModelRoute)
        .where(
            ModelRoute.project_id.is_(None),
            ModelRoute.task_key == task_key,
            ModelRoute.enabled == True,  # noqa: E712
        )
        .order_by(ModelRoute.priority)
    )
    rows.extend(session.exec(stmt_global).all())
    return rows


def resolve(session: Session, task_key: str, project_id: Optional[uuid.UUID] = None) -> ModelRoute:
    """Return the primary route for a task. Raises NoRouteError if none configured."""
    chain = resolve_chain(session, task_key, project_id)
    if not chain:
        raise NoRouteError(f"no enabled model_route for task_key={task_key!r}")
    return chain[0]


def list_for_project(session: Session, project_id: Optional[uuid.UUID]) -> List[ModelRoute]:
    """List all routes for a project (or global routes when project_id is None)."""
    stmt = select(ModelRoute)
    if project_id is None:
        stmt = stmt.where(ModelRoute.project_id.is_(None))
    else:
        stmt = stmt.where(ModelRoute.project_id == project_id)
    stmt = stmt.order_by(ModelRoute.task_key, ModelRoute.priority)
    return list(session.exec(stmt).all())


def get(session: Session, route_id: uuid.UUID) -> ModelRoute:
    row = session.get(ModelRoute, route_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="model_route not found")
    return row


def create(session: Session, project_id: Optional[uuid.UUID], payload: ModelRouteCreate, created_by: str = "api") -> ModelRoute:
    row = ModelRoute(
        project_id=project_id,
        task_key=payload.task_key,
        provider=payload.provider,
        model_id=payload.model_id,
        params=payload.params,
        priority=payload.priority,
        enabled=payload.enabled,
        cost_unit=payload.cost_unit,
        cost_per_unit_usd=payload.cost_per_unit_usd,
        pricing_updated_at=datetime.now(timezone.utc) if payload.cost_per_unit_usd is not None else None,
        created_by=created_by,
    )
    session.add(row)
    session.flush()
    session.refresh(row)
    return row


def update(session: Session, route_id: uuid.UUID, payload: ModelRouteUpdate) -> ModelRoute:
    row = get(session, route_id)
    data = payload.model_dump(exclude_unset=True)
    if "cost_per_unit_usd" in data and data["cost_per_unit_usd"] is not None:
        row.pricing_updated_at = datetime.now(timezone.utc)
    for key, value in data.items():
        setattr(row, key, value)
    session.add(row)
    session.flush()
    session.refresh(row)
    return row


def delete(session: Session, route_id: uuid.UUID) -> None:
    row = get(session, route_id)
    session.delete(row)
    session.flush()
