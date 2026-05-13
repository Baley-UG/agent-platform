"""Service-layer logic for ig_scan_targets.

The CRUD surface is mostly straightforward; the interesting pieces are:
- `enqueue_jobs_for_due_targets` — used by the scheduler. Batches due
  targets, creates the right job(s) per target (full vs incremental
  for users; top/recent for hashtags), bumps next_run_at with jitter.
- `run_now` — operator-triggered "scrape this target right now",
  doesn't change next_run_at.
- `activate` — flips a `pending_review` target (auto-discovered from
  hashtag scans) to `active` so the scheduler picks it up.
"""

import random
import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.core.config import settings
from app.core.logging import logger
from app.models.job import ScrapeJob
from app.models.target import ScanTarget
from app.schemas.targets import TargetCreate, TargetRead, TargetUpdate

VALID_KINDS = {"user", "hashtag"}
VALID_STATUSES = {"active", "paused", "pending_review"}
VALID_HASHTAG_SECTIONS = {"top", "recent"}


class TargetConflictError(Exception):
    """Raised when uniqueness on (kind, value) is violated."""


class TargetNotFoundError(Exception):
    """Raised when a lookup returns nothing."""


class InvalidTargetStateError(Exception):
    """Raised on bad enum value or illegal transition."""


def _to_read(target: ScanTarget) -> TargetRead:
    return TargetRead(
        id=target.id,
        kind=target.kind,
        value=target.value,
        status=target.status,
        interval_hours=target.interval_hours,
        fetch_feed=target.fetch_feed,
        fetch_stories=target.fetch_stories,
        fetch_highlights=target.fetch_highlights,
        fetch_comments=target.fetch_comments,
        comment_limit=target.comment_limit,
        min_likes=target.min_likes,
        min_impressions=target.min_impressions,
        hashtag_section=target.hashtag_section,
        first_backfill_done=target.first_backfill_done,
        last_seen_post_id=target.last_seen_post_id,
        last_seen_taken_at=target.last_seen_taken_at,
        last_run_at=target.last_run_at,
        next_run_at=target.next_run_at,
        last_run_job_id=target.last_run_job_id,
        auto_discovered=target.auto_discovered,
        source_target_id=target.source_target_id,
        created_at=target.created_at,
        updated_at=target.updated_at,
    )


def _normalise_value(kind: str, value: str) -> str:
    """Targets are case-insensitive — store lowercased so uniqueness fires."""
    return value.lstrip("#").lstrip("@").strip().lower()


# ----------------------------------------------------------------------
# CRUD
# ----------------------------------------------------------------------


def create_target(session: Session, payload: TargetCreate) -> TargetRead:
    """Insert a new tracked target."""
    if payload.kind not in VALID_KINDS:
        raise InvalidTargetStateError(f"kind must be one of {sorted(VALID_KINDS)}")
    if payload.status not in VALID_STATUSES:
        raise InvalidTargetStateError(f"status must be one of {sorted(VALID_STATUSES)}")
    if payload.hashtag_section not in VALID_HASHTAG_SECTIONS:
        raise InvalidTargetStateError(
            f"hashtag_section must be one of {sorted(VALID_HASHTAG_SECTIONS)}"
        )

    target = ScanTarget(
        kind=payload.kind,
        value=_normalise_value(payload.kind, payload.value),
        status=payload.status,
        interval_hours=payload.interval_hours,
        fetch_feed=payload.fetch_feed,
        fetch_stories=payload.fetch_stories,
        fetch_highlights=payload.fetch_highlights,
        fetch_comments=payload.fetch_comments,
        comment_limit=payload.comment_limit,
        min_likes=payload.min_likes,
        min_impressions=payload.min_impressions,
        hashtag_section=payload.hashtag_section,
        next_run_at=datetime.now(timezone.utc),
    )
    session.add(target)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise TargetConflictError(
            f"target ({payload.kind}, {target.value}) already exists"
        ) from exc

    logger.info(
        "target_created",
        target_id=str(target.id),
        kind=target.kind,
        value=target.value,
        status=target.status,
    )
    return _to_read(target)


def list_targets(
    session: Session,
    *,
    kind: Optional[str] = None,
    status: Optional[str] = None,
    auto_discovered: Optional[bool] = None,
) -> List[TargetRead]:
    """Filtered list of tracked targets, newest first."""
    stmt = select(ScanTarget).order_by(ScanTarget.created_at.desc())
    if kind is not None:
        if kind not in VALID_KINDS:
            raise InvalidTargetStateError(f"kind must be one of {sorted(VALID_KINDS)}")
        stmt = stmt.where(ScanTarget.kind == kind)
    if status is not None:
        if status not in VALID_STATUSES:
            raise InvalidTargetStateError(
                f"status must be one of {sorted(VALID_STATUSES)}"
            )
        stmt = stmt.where(ScanTarget.status == status)
    if auto_discovered is not None:
        stmt = stmt.where(ScanTarget.auto_discovered == auto_discovered)
    return [_to_read(t) for t in session.exec(stmt).all()]


def _get_or_raise(session: Session, target_id: uuid.UUID) -> ScanTarget:
    target = session.get(ScanTarget, target_id)
    if target is None:
        raise TargetNotFoundError(str(target_id))
    return target


def get_target(session: Session, target_id: uuid.UUID) -> TargetRead:
    return _to_read(_get_or_raise(session, target_id))


def update_target(
    session: Session, target_id: uuid.UUID, payload: TargetUpdate
) -> TargetRead:
    target = _get_or_raise(session, target_id)
    if payload.status is not None:
        if payload.status not in VALID_STATUSES:
            raise InvalidTargetStateError(
                f"status must be one of {sorted(VALID_STATUSES)}"
            )
        target.status = payload.status
    if payload.hashtag_section is not None:
        if payload.hashtag_section not in VALID_HASHTAG_SECTIONS:
            raise InvalidTargetStateError(
                f"hashtag_section must be one of {sorted(VALID_HASHTAG_SECTIONS)}"
            )
        target.hashtag_section = payload.hashtag_section
    if payload.interval_hours is not None:
        target.interval_hours = payload.interval_hours
    for field in (
        "fetch_feed",
        "fetch_stories",
        "fetch_highlights",
        "fetch_comments",
        "comment_limit",
        "min_likes",
        "min_impressions",
    ):
        new_value = getattr(payload, field)
        if new_value is not None:
            setattr(target, field, new_value)
    target.updated_at = datetime.now(timezone.utc)
    session.add(target)
    session.flush()
    logger.info("target_updated", target_id=str(target.id))
    return _to_read(target)


def activate_target(session: Session, target_id: uuid.UUID) -> TargetRead:
    """Flip a `pending_review` target to `active`. Idempotent for already-active rows."""
    target = _get_or_raise(session, target_id)
    target.status = "active"
    target.updated_at = datetime.now(timezone.utc)
    session.add(target)
    session.flush()
    logger.info("target_activated", target_id=str(target.id), value=target.value)
    return _to_read(target)


def pause_target(session: Session, target_id: uuid.UUID) -> TargetRead:
    target = _get_or_raise(session, target_id)
    target.status = "paused"
    target.updated_at = datetime.now(timezone.utc)
    session.add(target)
    session.flush()
    logger.info("target_paused", target_id=str(target.id), value=target.value)
    return _to_read(target)


# ----------------------------------------------------------------------
# Job creation primitives (used by scheduler + run-now endpoint)
# ----------------------------------------------------------------------


def _job_types_for_target(target: ScanTarget) -> List[str]:
    """Decide which job_type(s) to enqueue for this target right now."""
    if target.kind == "user":
        primary = "user_feed_full" if not target.first_backfill_done else "user_feed_incremental"
        types = [primary]
        if target.fetch_stories:
            types.append("user_stories")
        if target.fetch_highlights and not target.first_backfill_done:
            # Highlights default to "scan once on first add"; later
            # highlight runs are operator-triggered or scheduler-bumped.
            types.append("user_highlights")
        return types
    if target.kind == "hashtag":
        return [f"hashtag_{target.hashtag_section}"]
    return []


def _build_job(target: ScanTarget, job_type: str) -> ScrapeJob:
    return ScrapeJob(
        job_type=job_type,
        target=target.value,
        scan_target_id=target.id,
        priority=100,
        params={
            "fetch_comments": target.fetch_comments,
            "comment_limit": target.comment_limit,
            "auto_enrich_users": False,
        },
        min_likes=target.min_likes,
        min_impressions=target.min_impressions,
        scheduled_for=datetime.now(timezone.utc),
        max_attempts=3,
    )


def _bumped_next_run_at(target: ScanTarget) -> datetime:
    """now() + interval_hours ± jitter%. Jitter spreads the daily fleet."""
    base = datetime.now(timezone.utc) + timedelta(hours=target.interval_hours)
    jitter_pct = settings.IG_TARGET_INTERVAL_JITTER_PCT / 100.0
    jitter_seconds = (
        target.interval_hours * 3600 * jitter_pct * (random.random() - 0.5) * 2
    )
    return base + timedelta(seconds=jitter_seconds)


def run_now(session: Session, target_id: uuid.UUID) -> List[ScrapeJob]:
    """Enqueue the job(s) for `target_id` immediately, no cursor bump.

    Idempotency is the caller's problem — pressing this button twice
    will create two parallel jobs. The worker's account_pool will
    serialise them on the same account if the scope overlaps.
    """
    target = _get_or_raise(session, target_id)
    jobs: List[ScrapeJob] = []
    for job_type in _job_types_for_target(target):
        job = _build_job(target, job_type)
        session.add(job)
        jobs.append(job)
    session.flush()
    logger.info(
        "target_run_now",
        target_id=str(target.id),
        value=target.value,
        job_count=len(jobs),
    )
    return jobs


def enqueue_jobs_for_due_targets(session: Session, *, batch_size: int = 100) -> int:
    """Scheduler tick body — enqueue jobs for any targets whose
    next_run_at has passed.

    Returns the total job count created. Bumps next_run_at on every
    target it touches so the same target isn't re-enqueued on the next
    tick before the worker picks it up.
    """
    from sqlalchemy import text as sql_text

    now = datetime.now(timezone.utc)
    rows = session.execute(
        sql_text(
            """
            SELECT id FROM ig_scan_targets
            WHERE status = 'active' AND next_run_at <= :now
            ORDER BY next_run_at ASC
            FOR UPDATE SKIP LOCKED
            LIMIT :batch_size
            """
        ),
        {"now": now, "batch_size": batch_size},
    ).all()

    total_jobs = 0
    for (target_id,) in rows:
        target = session.get(ScanTarget, target_id)
        if target is None:
            continue
        for job_type in _job_types_for_target(target):
            job = _build_job(target, job_type)
            session.add(job)
            total_jobs += 1
        target.next_run_at = _bumped_next_run_at(target)
        target.updated_at = now
        session.add(target)

    if total_jobs:
        logger.info(
            "scheduler_enqueued_jobs",
            targets=len(rows),
            jobs=total_jobs,
        )
    return total_jobs
