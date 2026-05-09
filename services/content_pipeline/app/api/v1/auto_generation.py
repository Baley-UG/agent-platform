"""auto_generation_rules CRUD + manual `run-now` trigger."""

from __future__ import annotations

import uuid
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session, select

from app.api.v1.deps import get_project, get_session, require_api_key
from app.models.auto_generation_rules import AutoGenerationRule
from app.models.projects import Project
from app.schemas.auto_generation import (
    AutoGenRuleCreate,
    AutoGenRuleRead,
    AutoGenRuleRunResponse,
    AutoGenRuleUpdate,
)
from app.services import auto_generation as svc

router = APIRouter(
    prefix="/projects/{project_id}/auto-generation-rules",
    tags=["auto-generation"],
    dependencies=[Depends(require_api_key)],
)


def _scoped(session: Session, project: Project, rule_id: uuid.UUID) -> AutoGenerationRule:
    rule = session.get(AutoGenerationRule, rule_id)
    if rule is None or rule.project_id != project.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="rule not found")
    return rule


@router.post("", response_model=AutoGenRuleRead, status_code=status.HTTP_201_CREATED)
def create(
    payload: AutoGenRuleCreate,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> AutoGenRuleRead:
    rule = AutoGenerationRule(project_id=project.id, **payload.model_dump())
    session.add(rule)
    session.flush()
    session.refresh(rule)
    return AutoGenRuleRead.model_validate(rule)


@router.get("", response_model=List[AutoGenRuleRead])
def list_(
    project: Project = Depends(get_project), session: Session = Depends(get_session)
) -> List[AutoGenRuleRead]:
    rows = session.exec(
        select(AutoGenerationRule).where(AutoGenerationRule.project_id == project.id)
    ).all()
    return [AutoGenRuleRead.model_validate(r) for r in rows]


@router.get("/{rule_id}", response_model=AutoGenRuleRead)
def get_rule(
    rule_id: uuid.UUID, project: Project = Depends(get_project), session: Session = Depends(get_session)
) -> AutoGenRuleRead:
    return AutoGenRuleRead.model_validate(_scoped(session, project, rule_id))


@router.patch("/{rule_id}", response_model=AutoGenRuleRead)
def update(
    rule_id: uuid.UUID,
    payload: AutoGenRuleUpdate,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> AutoGenRuleRead:
    rule = _scoped(session, project, rule_id)
    for key, value in payload.model_dump(exclude_unset=True).items():
        if value is not None:
            setattr(rule, key, value)
    session.add(rule)
    session.flush()
    session.refresh(rule)
    return AutoGenRuleRead.model_validate(rule)


@router.delete("/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete(
    rule_id: uuid.UUID, project: Project = Depends(get_project), session: Session = Depends(get_session)
) -> None:
    rule = _scoped(session, project, rule_id)
    session.delete(rule)
    session.flush()


@router.post("/{rule_id}/run-now", response_model=AutoGenRuleRunResponse)
def run_now(
    rule_id: uuid.UUID,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> AutoGenRuleRunResponse:
    """Bypass the hourly cron and try to spawn one scenario from this rule."""
    rule = _scoped(session, project, rule_id)
    new_id = svc.run_rule(session, rule, project)
    if new_id is None:
        return AutoGenRuleRunResponse(
            rule_id=rule.id,
            spawned_scenario_id=None,
            reason="rule disabled, daily_quota reached, budget exhausted, or no candidate references",
        )
    return AutoGenRuleRunResponse(rule_id=rule.id, spawned_scenario_id=new_id)
