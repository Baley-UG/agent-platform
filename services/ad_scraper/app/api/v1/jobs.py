"""Ingestion job endpoints.

`POST /jobs` takes the `materialList` GraphQL variables verbatim, so an
operator can build a query in the AppGrowing UI, copy the variables out of
the network tab, and paste them here. The page window is validated against
the API's 200-page ceiling up front (see `schemas.jobs.JobCreate`) — a
request that could never succeed is rejected as a 422 rather than
discovered mid-run.
"""

import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlmodel import Session

from app.api.v1.deps import get_read_session, get_session, require_api_key
from app.models.job import VALID_STATUSES
from app.schemas.jobs import JobCreate, JobRead
from app.services import jobs as jobs_service

router = APIRouter(dependencies=[Depends(require_api_key)])


@router.post("", response_model=JobRead, status_code=status.HTTP_201_CREATED)
def create_job(payload: JobCreate, session: Session = Depends(get_session)) -> JobRead:
    """Enqueue an ingestion job. The worker picks it up within a poll cycle."""
    job = jobs_service.create_job(
        session,
        filters=payload.filters,
        page_from=payload.page_from,
        page_to=payload.page_to,
        order=payload.order,
        mirror=payload.mirror,
        max_attempts=payload.max_attempts,
    )
    return JobRead.model_validate(job)


@router.get("", response_model=List[JobRead])
def list_jobs(
    status_: Optional[str] = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_read_session),
) -> List[JobRead]:
    """List jobs, newest first."""
    if status_ and status_ not in VALID_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"status must be one of {sorted(VALID_STATUSES)}",
        )
    rows = jobs_service.list_jobs(session, status=status_, limit=limit, offset=offset)
    return [JobRead.model_validate(row) for row in rows]


@router.get("/{job_id}", response_model=JobRead)
def get_job(job_id: uuid.UUID, session: Session = Depends(get_read_session)) -> JobRead:
    """Fetch one job, including its `stats` counters."""
    job = jobs_service.get_job(session, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job not found")
    return JobRead.model_validate(job)


@router.post("/{job_id}/cancel", response_model=JobRead)
def cancel_job(job_id: uuid.UUID, session: Session = Depends(get_session)) -> JobRead:
    """Cancel a queued job. Already-terminal jobs are returned unchanged."""
    job = jobs_service.cancel_job(session, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job not found")
    return JobRead.model_validate(job)


@router.post("/{job_id}/retry", response_model=JobRead)
def retry_job(job_id: uuid.UUID, session: Session = Depends(get_session)) -> JobRead:
    """Requeue a job with a fresh attempt budget."""
    job = jobs_service.retry_job(session, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job not found")
    return JobRead.model_validate(job)
