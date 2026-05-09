"""posting_strategy GET/PUT — one row per project, lazy-create on first read."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlmodel import Session

from app.api.v1.deps import get_project, get_session, require_api_key
from app.models.projects import Project
from app.schemas.posting_strategy import PostingStrategyRead, PostingStrategyUpdate
from app.services import posting_strategy as svc

router = APIRouter(
    prefix="/projects/{project_id}/posting-strategy",
    tags=["posting-strategy"],
    dependencies=[Depends(require_api_key)],
)


@router.get("", response_model=PostingStrategyRead)
def get_strategy(
    project: Project = Depends(get_project), session: Session = Depends(get_session)
) -> PostingStrategyRead:
    return PostingStrategyRead.model_validate(svc.get_or_create(session, project.id))


@router.put("", response_model=PostingStrategyRead)
def put_strategy(
    payload: PostingStrategyUpdate,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> PostingStrategyRead:
    return PostingStrategyRead.model_validate(svc.update(session, project.id, payload.model_dump(exclude_unset=True)))
