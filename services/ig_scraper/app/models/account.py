"""ig_accounts — Instagram accounts we control for scraping."""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import LargeBinary
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Column, Field, SQLModel

from app.models.base import new_uuid, utcnow


class Account(SQLModel, table=True):
    """A scraping account.

    Sticky to one Proxy, one device fingerprint. Status drives whether
    the pool will pick it. `quota_tier` (`fresh` / `mid` / `warm`) feeds
    the daily request cap; `cooldown_until` is set after rate-limit
    signals.
    """

    __tablename__ = "ig_accounts"

    id: uuid.UUID = Field(default_factory=new_uuid, primary_key=True)
    username: str = Field(unique=True, index=True)
    password_enc: bytes = Field(sa_column=Column(LargeBinary, nullable=False))
    session_blob: Optional[dict] = Field(default=None, sa_column=Column(JSONB, nullable=True))
    status: str = Field(default="disabled")  # active | cooldown | challenge_required | banned | disabled
    role: str = Field(default="scraper")  # scraper | canary
    proxy_id: Optional[uuid.UUID] = Field(default=None, foreign_key="ig_proxies.id")
    timezone: str = Field(default="UTC")
    active_hours_start: int = Field(default=8)
    active_hours_end: int = Field(default=23)
    weekday_pattern: int = Field(default=127, description="Bitmap Mon=1..Sun=64; 127 = all days.")
    quota_tier: str = Field(default="fresh")
    cooldown_until: Optional[datetime] = Field(default=None)
    last_used_at: Optional[datetime] = Field(default=None)
    last_login_at: Optional[datetime] = Field(default=None)
    failure_count: int = Field(default=0)
    notes: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
