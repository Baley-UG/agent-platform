"""Jobs router — full CRUD + cancel/retry (M3)."""

import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.v1.deps import require_api_key
from app.schemas.jobs import JobCreate, JobRead
from app.services import jobs as jobs_service
from app.services.database import session_scope

router = APIRouter(dependencies=[Depends(require_api_key)])


def _to_read(job) -> JobRead:
    """Adapter from the SQLModel row to the API schema."""
    return JobRead.model_validate(job, from_attributes=True)


@router.post("", response_model=JobRead, status_code=status.HTTP_201_CREATED)
def create_job(payload: JobCreate) -> JobRead:
    """Enqueue a scan job."""
    try:
        with session_scope() as session:
            job = jobs_service.create_job(session, payload)
            return _to_read(job)
    except jobs_service.InvalidJobStateError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.get("", response_model=List[JobRead])
def list_jobs(
    status_filter: Optional[str] = Query(default=None, alias="status"),
    job_type: Optional[str] = Query(default=None),
    target: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> List[JobRead]:
    """List jobs with optional filters."""
    try:
        with session_scope() as session:
            rows = jobs_service.list_jobs(
                session,
                status=status_filter,
                job_type=job_type,
                target=target,
                limit=limit,
                offset=offset,
            )
            return [_to_read(j) for j in rows]
    except jobs_service.InvalidJobStateError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.get("/{job_id}", response_model=JobRead)
def get_job(job_id: uuid.UUID) -> JobRead:
    """Fetch a single job."""
    try:
        with session_scope() as session:
            return _to_read(jobs_service.get_job(session, job_id))
    except jobs_service.JobNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="job not found")


@router.post("/{job_id}/cancel", response_model=JobRead)
def cancel_job(job_id: uuid.UUID) -> JobRead:
    """Cancel a queued (or running, cooperatively) job."""
    try:
        with session_scope() as session:
            return _to_read(jobs_service.cancel_job(session, job_id))
    except jobs_service.JobNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="job not found")
    except jobs_service.InvalidJobStateError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc))


@router.post("/{job_id}/retry", response_model=JobRead)
def retry_job(job_id: uuid.UUID) -> JobRead:
    """Re-queue a failed job."""
    try:
        with session_scope() as session:
            return _to_read(jobs_service.retry_job(session, job_id))
    except jobs_service.JobNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="job not found")
    except jobs_service.InvalidJobStateError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc))
