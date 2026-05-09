"""publish_jobs orchestration helpers (CRUD + state transitions)."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Session, select

from app.core import security
from app.models.plan_slots import PlanSlot
from app.models.publish_jobs import PublishJob
from app.models.social_accounts import SocialAccount


def create_pending(session: Session, slot: PlanSlot) -> PublishJob:
    """Insert a new publish_job row in `pending`. Caller enqueues the worker."""
    if slot.social_account_id is None:
        raise ValueError("plan_slot has no social_account_id")
    account = session.get(SocialAccount, slot.social_account_id)
    if account is None:
        raise ValueError(f"social_account {slot.social_account_id} missing")
    job = PublishJob(
        plan_slot_id=slot.id,
        social_account_id=account.id,
        provider=account.provider,
        status="pending",
    )
    session.add(job)
    session.flush()
    slot.publish_job_id = job.id
    slot.status = "scheduled"
    session.add(slot)
    session.flush()
    return job


def mark_uploading(session: Session, job: PublishJob, container_id: Optional[str] = None) -> PublishJob:
    job.status = "uploading"
    job.attempts += 1
    if container_id is not None:
        job.provider_container_id = container_id
    session.add(job)
    session.flush()
    return job


def mark_processing(session: Session, job: PublishJob) -> PublishJob:
    job.status = "processing"
    session.add(job)
    session.flush()
    return job


def mark_published(session: Session, job: PublishJob, *, media_id: Optional[str], response: dict) -> PublishJob:
    job.status = "published"
    job.provider_media_id = media_id
    job.response = response
    job.published_at = datetime.now(timezone.utc)
    session.add(job)
    session.flush()
    return job


def mark_failed(session: Session, job: PublishJob, error: str) -> PublishJob:
    job.status = "failed"
    job.last_error = error[:2000]
    session.add(job)
    session.flush()
    return job


def get_credentials(session: Session, account: SocialAccount) -> dict:
    if account.credentials_encrypted is None:
        return {}
    raw = security.decrypt(account.credentials_encrypted)
    return json.loads(raw)


def list_for_slot(session: Session, slot_id: uuid.UUID) -> list[PublishJob]:
    stmt = select(PublishJob).where(PublishJob.plan_slot_id == slot_id).order_by(PublishJob.created_at.desc())
    return list(session.exec(stmt).all())
