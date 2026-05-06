"""ig_webhook_deliveries — per-attempt log for outbound webhook calls."""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Column, Field, SQLModel

from app.models.base import new_uuid, utcnow


class WebhookDelivery(SQLModel, table=True):
    """One row per webhook delivery attempt.

    `status` ∈ {pending, in_flight, delivered, failed}. The dispatcher
    claims rows with `status='pending'` and `scheduled_for <= now()`
    via `FOR UPDATE SKIP LOCKED`, fires them, and either marks
    `delivered` or bumps `attempt` and re-schedules with backoff.
    """

    __tablename__ = "ig_webhook_deliveries"

    id: uuid.UUID = Field(default_factory=new_uuid, primary_key=True)
    webhook_id: uuid.UUID = Field(foreign_key="ig_webhooks.id")
    event_type: str
    payload: dict = Field(sa_column=Column(JSONB, nullable=False))
    status: str = Field(default="pending")
    attempt: int = Field(default=0)
    max_attempts: int = Field(default=5)
    scheduled_for: datetime = Field(default_factory=utcnow)
    last_attempt_at: Optional[datetime] = Field(default=None)
    response_status: Optional[int] = Field(default=None)
    error: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=utcnow)
