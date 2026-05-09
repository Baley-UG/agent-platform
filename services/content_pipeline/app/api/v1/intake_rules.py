"""reference_intake_rules CRUD + simple inbox/candidates endpoint."""

from __future__ import annotations

import uuid
from typing import List

from fastapi import APIRouter, Depends, status
from sqlmodel import Session

from app.api.v1.deps import get_project, get_session, require_api_key
from app.models.projects import Project
from app.schemas.intake_rules import IntakeRuleCreate, IntakeRuleRead, IntakeRuleUpdate
from app.schemas.references import ReferenceRead
from app.services import intake_rules as svc
from app.services import references as references_svc

router = APIRouter(
    prefix="/projects/{project_id}",
    tags=["intake-rules"],
    dependencies=[Depends(require_api_key)],
)


@router.post("/intake-rules", response_model=IntakeRuleRead, status_code=status.HTTP_201_CREATED)
def create(
    payload: IntakeRuleCreate,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> IntakeRuleRead:
    return IntakeRuleRead.model_validate(svc.create(session, project.id, payload))


@router.get("/intake-rules", response_model=List[IntakeRuleRead])
def list_(project: Project = Depends(get_project), session: Session = Depends(get_session)) -> List[IntakeRuleRead]:
    return [IntakeRuleRead.model_validate(r) for r in svc.list_(session, project.id)]


@router.patch("/intake-rules/{rule_id}", response_model=IntakeRuleRead)
def update(
    rule_id: uuid.UUID,
    payload: IntakeRuleUpdate,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> IntakeRuleRead:
    return IntakeRuleRead.model_validate(svc.update(session, project.id, rule_id, payload))


@router.delete("/intake-rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete(
    rule_id: uuid.UUID,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> None:
    svc.delete(session, project.id, rule_id)


# ----- Inbox: references awaiting admin review -----


@router.get("/inbox/candidates", response_model=List[ReferenceRead])
def inbox_candidates(
    project: Project = Depends(get_project), session: Session = Depends(get_session)
) -> List[ReferenceRead]:
    """Shortcut: references with status='candidate' (awaiting admin approval)."""
    return [
        ReferenceRead.model_validate(r)
        for r in references_svc.list_(session, project.id, status_="candidate", limit=200)
    ]
