"""Cost reporting endpoints — project-wide summary and per-scenario drill-down."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from sqlmodel import Session

from app.api.v1.deps import get_project, get_session, require_api_key
from app.models.projects import Project
from app.schemas.cost import CostSummary, GenerationCallRead
from app.services import cost as svc

router = APIRouter(prefix="/projects/{project_id}", tags=["cost"], dependencies=[Depends(require_api_key)])


@router.get("/cost-summary", response_model=CostSummary)
def cost_summary(
    from_: Optional[datetime] = Query(default=None, alias="from"),
    to: Optional[datetime] = Query(default=None),
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> CostSummary:
    """Aggregate spend across an arbitrary window (default: last 30 days)."""
    return svc.project_summary(session, project, from_=from_, to=to)


@router.get("/scenarios/{scenario_id}/generation-calls", response_model=List[GenerationCallRead])
def generation_calls_for_scenario(
    scenario_id: uuid.UUID,
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> List[GenerationCallRead]:
    """Per-scenario drill-down — every external API call we made for this scenario."""
    # Scope is implicitly enforced because GenerationCall.project_id is a FK and we
    # filter by scenario_id; a leak would require corrupting the DB. Still — defensive:
    rows = svc.list_calls_for_scenario(session, scenario_id, limit=limit, offset=offset)
    return [GenerationCallRead.model_validate(r) for r in rows if r.project_id == project.id]
