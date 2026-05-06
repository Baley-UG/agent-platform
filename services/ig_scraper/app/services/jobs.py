"""Service-layer logic for ig_scrape_jobs.

Two distinct surfaces share this module:
- **API surface** — create_job/list_jobs/get_job/cancel_job/retry_job.
  Synchronous, called from FastAPI handlers.
- **Worker surface** — claim_next_job/mark_succeeded/mark_failed/
  mark_retry. Synchronous, called from the worker loop. The worker
  loop runs them inside its own transaction so the SKIP LOCKED hold is
  released as soon as the row is updated.

`claim_next_job` is the single most-load-bearing query in the service.
It MUST use `FOR UPDATE SKIP LOCKED` so multiple worker replicas can
poll the queue without contending on the same row.
"""

import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from sqlalchemy import text
from sqlmodel import Session, select

from app.core.logging import logger
from app.models.job import ScrapeJob
from app.schemas.jobs import JobCreate

# Set of valid job types. Mirrors `JobType` literal in schemas/jobs.py
# but kept as a frozenset here so service-layer callers (worker, tests)
# don't need to import pydantic.
VALID_JOB_TYPES = frozenset(
    {
        "user_feed_full",
        "user_feed_incremental",
        "user_stories",
        "user_highlights",
        "hashtag_top",
        "hashtag_recent",
        "user_enrich",
        "embed_post_batch",
        "extract_llm_features_batch",
    }
)
TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled"})


class JobNotFoundError(Exception):
    """Raised when a lookup by id returns nothing."""


class InvalidJobStateError(Exception):
    """Raised on bad job_type / status / illegal transition."""


# ----------------------------------------------------------------------
# API-surface helpers
# ----------------------------------------------------------------------


def create_job(session: Session, payload: JobCreate) -> ScrapeJob:
    """Insert a queued job. Returns the persisted row."""
    if payload.job_type not in VALID_JOB_TYPES:
        raise InvalidJobStateError(f"unknown job_type '{payload.job_type}'")

    job = ScrapeJob(
        job_type=payload.job_type,
        target=payload.target,
        priority=payload.priority,
        params=payload.params,
        min_likes=payload.min_likes,
        min_impressions=payload.min_impressions,
        max_attempts=payload.max_attempts,
        scheduled_for=payload.scheduled_for or datetime.now(timezone.utc),
    )
    session.add(job)
    session.flush()
    logger.info(
        "job_created",
        job_id=str(job.id),
        job_type=job.job_type,
        target=job.target,
        priority=job.priority,
    )
    return job


def list_jobs(
    session: Session,
    *,
    status: Optional[str] = None,
    job_type: Optional[str] = None,
    target: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> List[ScrapeJob]:
    """Filter + paginate. Worker columns (account_id, attempt, etc.) are
    included so an operator can quickly see who claimed what."""
    stmt = select(ScrapeJob).order_by(ScrapeJob.created_at.desc())
    if status is not None:
        stmt = stmt.where(ScrapeJob.status == status)
    if job_type is not None:
        if job_type not in VALID_JOB_TYPES:
            raise InvalidJobStateError(f"unknown job_type '{job_type}'")
        stmt = stmt.where(ScrapeJob.job_type == job_type)
    if target is not None:
        stmt = stmt.where(ScrapeJob.target == target)
    stmt = stmt.offset(max(0, offset)).limit(min(max(1, limit), 500))
    return list(session.exec(stmt).all())


def get_job(session: Session, job_id: uuid.UUID) -> ScrapeJob:
    job = session.get(ScrapeJob, job_id)
    if job is None:
        raise JobNotFoundError(str(job_id))
    return job


def cancel_job(session: Session, job_id: uuid.UUID) -> ScrapeJob:
    """Set status='cancelled' if still queued. Running jobs are
    cooperatively cancelled — we flip the status and trust the worker
    to check it on the next loop iteration."""
    job = get_job(session, job_id)
    if job.status in TERMINAL_STATUSES:
        raise InvalidJobStateError(f"job is already {job.status}")
    job.status = "cancelled"
    job.finished_at = datetime.now(timezone.utc)
    session.add(job)
    session.flush()
    logger.info("job_cancelled", job_id=str(job.id))
    return job


def retry_job(session: Session, job_id: uuid.UUID) -> ScrapeJob:
    """Re-queue a failed job. Resets status, error, started/finished_at,
    bumps `scheduled_for` to now."""
    job = get_job(session, job_id)
    if job.status != "failed":
        raise InvalidJobStateError(f"can only retry failed jobs (this one is {job.status})")
    job.status = "queued"
    job.error = None
    job.started_at = None
    job.finished_at = None
    job.scheduled_for = datetime.now(timezone.utc)
    session.add(job)
    session.flush()
    logger.info("job_retried", job_id=str(job.id), attempt=job.attempt)
    return job


# ----------------------------------------------------------------------
# Worker-surface helpers
# ----------------------------------------------------------------------

# The single most-load-bearing query in the service: claim the next
# queued job, atomically transition it to running, and return the full
# row. `FOR UPDATE SKIP LOCKED` lets multiple workers poll without
# blocking each other.
_CLAIM_SQL = text(
    """
    UPDATE ig_scrape_jobs
    SET status = 'running',
        started_at = now(),
        attempt = attempt + 1
    WHERE id = (
        SELECT id FROM ig_scrape_jobs
        WHERE status = 'queued' AND scheduled_for <= now()
        ORDER BY priority ASC, created_at ASC
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    )
    RETURNING id
    """
)


def claim_next_job(session: Session) -> Optional[ScrapeJob]:
    """Try to claim one queued job. Returns None if the queue is empty.

    The caller's transaction owns the row until commit/rollback. The
    worker loop commits immediately after this call so the row is
    visible in `running` status to /jobs queries.
    """
    row = session.execute(_CLAIM_SQL).first()
    if row is None:
        return None
    session.commit()
    job_id = row[0]
    return session.get(ScrapeJob, job_id)


def mark_succeeded(session: Session, job_id: uuid.UUID, stats: dict) -> None:
    """Transition running → succeeded, persist stats, set finished_at."""
    job = session.get(ScrapeJob, job_id)
    if job is None:
        raise JobNotFoundError(str(job_id))
    job.status = "succeeded"
    job.finished_at = datetime.now(timezone.utc)
    job.stats = stats
    job.error = None
    session.add(job)
    session.commit()
    logger.info("job_succeeded", job_id=str(job_id), stats=stats)


def mark_failed(session: Session, job_id: uuid.UUID, error: str) -> None:
    """Terminal failure — no automatic retry."""
    job = session.get(ScrapeJob, job_id)
    if job is None:
        raise JobNotFoundError(str(job_id))
    job.status = "failed"
    job.finished_at = datetime.now(timezone.utc)
    job.error = error[:4000]  # don't blow out the column with stack-trace soup
    session.add(job)
    session.commit()
    logger.error("job_failed", job_id=str(job_id), error=job.error)


def mark_retry(
    session: Session,
    job_id: uuid.UUID,
    error: str,
    backoff_seconds: int = 60,
) -> bool:
    """Soft failure — re-queue with backoff if attempts remain.

    Returns True if the job was re-queued, False if it transitioned to
    `failed` because max_attempts was exhausted.
    """
    job = session.get(ScrapeJob, job_id)
    if job is None:
        raise JobNotFoundError(str(job_id))
    if job.attempt >= job.max_attempts:
        job.status = "failed"
        job.finished_at = datetime.now(timezone.utc)
        job.error = error[:4000]
        session.add(job)
        session.commit()
        logger.error(
            "job_failed_after_retries",
            job_id=str(job_id),
            attempts=job.attempt,
            error=job.error,
        )
        return False

    job.status = "queued"
    job.scheduled_for = datetime.now(timezone.utc) + timedelta(seconds=backoff_seconds)
    job.started_at = None
    job.error = error[:4000]
    session.add(job)
    session.commit()
    logger.info(
        "job_requeued",
        job_id=str(job_id),
        attempt=job.attempt,
        next_run=job.scheduled_for.isoformat(),
    )
    return True
