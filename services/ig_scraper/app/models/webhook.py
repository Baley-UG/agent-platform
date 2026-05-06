"""ig_webhooks — outbound notification subscriptions."""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Column, Field, SQLModel

from app.models.base import new_uuid, utcnow


class Webhook(SQLModel, table=True):
    """A subscribed callback URL for a given event type.

    `event_type` examples (M9 will populate the dispatcher):
      - post_score_threshold (fires when a post crosses score >= threshold)
      - target_run_completed
      - account_challenge_required
    """

    __tablename__ = "ig_webhooks"

    id: uuid.UUID = Field(default_factory=new_uuid, primary_key=True)
    event_type: str = Field(index=True)
    url: str
    secret: Optional[str] = Field(default=None, description="HMAC-SHA256 signing secret.")
    filters: Optional[dict] = Field(default=None, sa_column=Column(JSONB, nullable=True))
    status: str = Field(default="active")  # active | paused | failing
    last_delivery_at: Optional[datetime] = Field(default=None)
    last_delivery_status: Optional[int] = Field(default=None)
    consecutive_failures: int = Field(default=0)
    created_at: datetime = Field(default_factory=utcnow)
