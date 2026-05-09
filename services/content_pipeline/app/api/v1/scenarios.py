"""scenarios endpoints — create, get, edit, approve, regenerate.

Create is a two-phase op: the row is inserted in `draft`, then the
analyzer worker is enqueued and the response carries `analyzer_job_id` so
the admin panel can poll progress.
"""

from __future__ import annotations

import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, Query, status
from sqlmodel import Session

from app.api.v1.deps import get_project, get_session, require_api_key
from app.models.projects import Project
from app.schemas.scenarios import ScenarioCreate, ScenarioRead, ScenarioUpdate
from app.services import queue
from app.services import scenarios as svc

router = APIRouter(
    prefix="/projects/{project_id}/scenarios",
    tags=["scenarios"],
    dependencies=[Depends(require_api_key)],
)


def _enqueue_analyzer(scenario_id: uuid.UUID) -> Optional[str]:
    """Push an analyzer job; tolerate Redis being unavailable in dev/test."""
    try:
        job = queue.enqueue("analyzer", "app.workers.analyzer.run", str(scenario_id))
        return job.id
    except Exception:  # noqa: BLE001
        # We log nothing here; queue.enqueue already increments the metric and
        # the admin panel sees `analyzer_job_id=None` when Redis is down.
        return None


@router.post("", response_model=ScenarioRead, status_code=status.HTTP_201_CREATED)
def create(
    payload: ScenarioCreate,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> ScenarioRead:
    """Create a draft scenario AND kick the analyzer.

    The analyzer call is best-effort — if Redis is unavailable, the scenario
    is still created in `draft` and the admin can retry via `/analyze`.
    """
    scenario = svc.create(session, project, payload, created_by="api")
    _enqueue_analyzer(scenario.id)
    return ScenarioRead.model_validate(scenario)


@router.post("/{scenario_id}/analyze", response_model=ScenarioRead)
def kick_analyzer(
    scenario_id: uuid.UUID,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> ScenarioRead:
    """Enqueue the analyzer for a scenario in `draft` or `analyzing`."""
    scenario = svc.get(session, project.id, scenario_id)
    if scenario.status not in ("draft", "analyzing", "failed"):
        return ScenarioRead.model_validate(scenario)
    _enqueue_analyzer(scenario.id)
    return ScenarioRead.model_validate(scenario)


@router.get("", response_model=List[ScenarioRead])
def list_(
    status_: Optional[str] = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> List[ScenarioRead]:
    return [
        ScenarioRead.model_validate(s)
        for s in svc.list_(session, project.id, status_=status_, limit=limit, offset=offset)
    ]


@router.get("/{scenario_id}", response_model=ScenarioRead)
def get(
    scenario_id: uuid.UUID,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> ScenarioRead:
    return ScenarioRead.model_validate(svc.get(session, project.id, scenario_id))


@router.patch("/{scenario_id}", response_model=ScenarioRead)
def update(
    scenario_id: uuid.UUID,
    payload: ScenarioUpdate,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> ScenarioRead:
    return ScenarioRead.model_validate(svc.update(session, project.id, scenario_id, payload))


@router.post("/{scenario_id}/approve", response_model=ScenarioRead)
def approve(
    scenario_id: uuid.UUID,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> ScenarioRead:
    return ScenarioRead.model_validate(svc.approve(session, project.id, scenario_id))


@router.post("/{scenario_id}/regenerate", response_model=ScenarioRead)
def regenerate(
    scenario_id: uuid.UUID,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> ScenarioRead:
    """Snapshot the current scenario_json into `previous_scenario_json`,
    bump `version`, and enqueue the analyzer."""
    scenario = svc.begin_regenerate(session, project.id, scenario_id)
    _enqueue_analyzer(scenario.id)
    return ScenarioRead.model_validate(scenario)
