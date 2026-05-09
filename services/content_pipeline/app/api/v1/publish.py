"""Publishing endpoints — `publish-now` for a slot, list publish jobs."""

from __future__ import annotations

import uuid
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlmodel import Session

from app.api.v1.deps import get_project, get_session, require_api_key
from app.models.plan_slots import PlanSlot
from app.models.projects import Project
from app.models.publish_jobs import PublishJob
from app.services import queue
from app.services import weekly_plans as plans_svc

router = APIRouter(
    prefix="/projects/{project_id}",
    tags=["publish"],
    dependencies=[Depends(require_api_key)],
)


class PublishJobRead(BaseModel):
    id: uuid.UUID
    plan_slot_id: uuid.UUID
    social_account_id: uuid.UUID
    provider: str
    provider_container_id: str | None
    provider_media_id: str | None
    status: str
    attempts: int
    last_error: str | None
    response: dict | None
    created_at: object
    published_at: object | None

    model_config = {"from_attributes": True}


@router.post("/plan-slots/{slot_id}/publish-now", response_model=PublishJobRead)
def publish_now(
    slot_id: uuid.UUID,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> PublishJobRead:
    """Bypass the scheduler and enqueue a publish job for this slot now."""
    slot = plans_svc.get_slot(session, project.id, slot_id)
    if slot.variant_id is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="slot has no variant_id")
    if slot.social_account_id is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="slot has no social_account_id")
    try:
        queue.enqueue("publish", "app.workers.publish.run", str(slot.id))
    except Exception:  # noqa: BLE001
        pass
    # Return the latest publish_job for this slot (worker will create one if missing).
    from sqlmodel import select

    job = session.exec(
        select(PublishJob).where(PublishJob.plan_slot_id == slot.id).order_by(PublishJob.created_at.desc())
    ).first()
    if job is None:
        # Worker hasn't run yet; surface a synthetic pending row by creating it eagerly
        from app.services import publishing as svc

        job = svc.create_pending(session, slot)
    return PublishJobRead.model_validate(job)


@router.get("/plan-slots/{slot_id}/publish-jobs", response_model=List[PublishJobRead])
def list_publish_jobs(
    slot_id: uuid.UUID,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> List[PublishJobRead]:
    slot = plans_svc.get_slot(session, project.id, slot_id)
    from app.services import publishing as svc

    return [PublishJobRead.model_validate(j) for j in svc.list_for_slot(session, slot.id)]
