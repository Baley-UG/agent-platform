"""Job queue CRUD and the worker's claim primitive.

The queue is the database. `claim_next_job` uses the
`UPDATE ... WHERE id = (SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1)`
idiom proven in ig_scraper: two workers can poll the same table
concurrently and neither will ever see the other's row, without a
distributed lock or a broker.

Scaling out is `replicas: 2+` in compose — SKIP LOCKED makes that safe
with no code change.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import text as sql_text
from sqlmodel import Session, select

from app.core.config import settings
from app.core.logging import logger
from app.core.metrics import ad_jobs_total
from app.models.job import (
    CANCELLED,
    FAILED,
    QUEUED,
    RUNNING,
    SUCCEEDED,
    TERMINAL_STATUSES,
    ScrapeJob,
)

# One statement so the SELECT ... FOR UPDATE SKIP LOCKED and the status
# flip cannot be separated by another worker's read.
_CLAIM_NEXT = sql_text("""
    UPDATE ad_scrape_jobs
       SET status     = 'running',
           attempt    = attempt + 1,
           started_at = :now
     WHERE id = (
        SELECT id
          FROM ad_scrape_jobs
         WHERE status = 'queued'
         ORDER BY created_at
           FOR UPDATE SKIP LOCKED
         LIMIT 1
     )
    RETURNING id
    """)


def create_job(
    session: Session,
    *,
    filters: Dict[str, Any],
    page_from: int = 1,
    page_to: Optional[int] = None,
    order: str = "max_dt_desc",
    mirror: Optional[bool] = None,
    max_attempts: int = 3,
) -> ScrapeJob:
    """Enqueue an ingestion job."""
    job = ScrapeJob(
        filters=dict(filters or {}),
        page_from=page_from,
        page_to=page_to if page_to is not None else settings.AD_DEFAULT_PAGE_TO,
        order=order,
        mirror=mirror,
        max_attempts=max_attempts,
        status=QUEUED,
    )
    session.add(job)
    session.flush()
    logger.info(
        "ad_job_created",
        job_id=str(job.id),
        page_from=job.page_from,
        page_to=job.page_to,
        order=job.order,
        filter_keys=sorted((filters or {}).keys()),
    )
    return job


def get_job(session: Session, job_id: uuid.UUID) -> Optional[ScrapeJob]:
    """Fetch one job by id."""
    return session.get(ScrapeJob, job_id)


def list_jobs(
    session: Session,
    *,
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> List[ScrapeJob]:
    """List jobs, newest first."""
    statement = select(ScrapeJob)
    if status:
        statement = statement.where(ScrapeJob.status == status)
    statement = statement.order_by(ScrapeJob.created_at.desc()).limit(limit).offset(offset)
    return list(session.exec(statement).all())


def claim_next_job(session: Session) -> Optional[ScrapeJob]:
    """Atomically claim the oldest queued job, or return None."""
    row = session.execute(_CLAIM_NEXT, {"now": datetime.now(timezone.utc)}).first()
    if row is None:
        return None
    return session.get(ScrapeJob, row[0])


def mark_succeeded(session: Session, job_id: uuid.UUID, stats: Optional[Dict[str, Any]] = None) -> None:
    """Terminal success."""
    job = session.get(ScrapeJob, job_id)
    if job is None:
        return
    job.status = SUCCEEDED
    job.stats = stats or {}
    job.error = None
    job.error_code = None
    job.finished_at = datetime.now(timezone.utc)
    session.add(job)
    ad_jobs_total.labels(status=SUCCEEDED).inc()
    logger.info("ad_job_succeeded", job_id=str(job_id), stats=stats)


def mark_failed(
    session: Session,
    job_id: uuid.UUID,
    error: str,
    *,
    error_code: Optional[str] = None,
    stats: Optional[Dict[str, Any]] = None,
) -> None:
    """Terminal failure — no further attempts."""
    job = session.get(ScrapeJob, job_id)
    if job is None:
        return
    job.status = FAILED
    job.error = error[:2000]
    job.error_code = error_code
    if stats is not None:
        job.stats = stats
    job.finished_at = datetime.now(timezone.utc)
    session.add(job)
    ad_jobs_total.labels(status=FAILED).inc()
    logger.warning("ad_job_failed", job_id=str(job_id), error_code=error_code, error=error[:300])


def mark_retry(
    session: Session,
    job_id: uuid.UUID,
    error: str,
    *,
    error_code: Optional[str] = None,
    stats: Optional[Dict[str, Any]] = None,
) -> None:
    """Requeue for another attempt, or fail terminally when exhausted.

    `attempt` was already incremented at claim time, so a job with
    `max_attempts=3` retries after its first and second failures and goes
    terminal on the third.
    """
    job = session.get(ScrapeJob, job_id)
    if job is None:
        return
    if job.attempt >= job.max_attempts:
        mark_failed(
            session,
            job_id,
            f"{error} (gave up after {job.attempt} attempts)",
            error_code=error_code,
            stats=stats,
        )
        return
    job.status = QUEUED
    job.error = error[:2000]
    job.error_code = error_code
    if stats is not None:
        job.stats = stats
    job.started_at = None
    session.add(job)
    logger.info(
        "ad_job_requeued",
        job_id=str(job_id),
        attempt=job.attempt,
        max_attempts=job.max_attempts,
        error_code=error_code,
    )


def cancel_job(session: Session, job_id: uuid.UUID) -> Optional[ScrapeJob]:
    """Cancel a non-terminal job. Returns None when the job doesn't exist."""
    job = session.get(ScrapeJob, job_id)
    if job is None:
        return None
    if job.status in TERMINAL_STATUSES:
        return job
    job.status = CANCELLED
    job.finished_at = datetime.now(timezone.utc)
    session.add(job)
    ad_jobs_total.labels(status=CANCELLED).inc()
    logger.info("ad_job_cancelled", job_id=str(job_id))
    return job


def retry_job(session: Session, job_id: uuid.UUID) -> Optional[ScrapeJob]:
    """Put a terminal job back in the queue with a fresh attempt budget.

    The attempt counter is reset rather than continued: an operator
    retrying by hand has usually fixed the cause (pasted a new cookie,
    narrowed the filter), so making them burn the remaining budget from
    the failed run would be surprising.
    """
    job = session.get(ScrapeJob, job_id)
    if job is None:
        return None
    job.status = QUEUED
    job.attempt = 0
    job.error = None
    job.error_code = None
    job.started_at = None
    job.finished_at = None
    session.add(job)
    logger.info("ad_job_retry_requested", job_id=str(job_id))
    return job


def requeue_stuck_jobs(session: Session) -> int:
    """Requeue jobs left `running` by a worker that died.

    Not on a timer today — there is no scheduler process. Called from the
    worker's startup so a container restart heals the queue, which is when
    stuck rows actually appear.
    """
    threshold = datetime.now(timezone.utc) - timedelta(minutes=settings.AD_JOB_STUCK_AFTER_MINUTES)
    result = session.execute(
        sql_text("""
            UPDATE ad_scrape_jobs
               SET status = 'queued', started_at = NULL
             WHERE status = 'running'
               AND started_at IS NOT NULL
               AND started_at < :threshold
            """),
        {"threshold": threshold},
    )
    count = result.rowcount or 0
    if count:
        logger.warning("ad_jobs_requeued_stuck", count=count, stuck_after_minutes=settings.AD_JOB_STUCK_AFTER_MINUTES)
    return count
