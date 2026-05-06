"""ig_proxies — generic HTTP/SOCKS5 proxy pool."""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import LargeBinary
from sqlmodel import Column, Field, SQLModel

from app.models.base import new_uuid, utcnow


class Proxy(SQLModel, table=True):
    """A proxy used by exactly one Account at a time.

    Sticky binding (account.proxy_id) is enforced at the application
    layer, not via FK constraint.
    """

    __tablename__ = "ig_proxies"

    id: uuid.UUID = Field(default_factory=new_uuid, primary_key=True)
    protocol: str  # 'http' | 'https' | 'socks5'
    host: str
    port: int
    username: Optional[str] = Field(default=None)
    password_enc: Optional[bytes] = Field(default=None, sa_column=Column(LargeBinary, nullable=True))
    label: Optional[str] = Field(default=None, description="Provider tag, e.g. 'brightdata-resi-dk'.")
    status: str = Field(default="active")  # active | cooldown | dead
    last_ok_at: Optional[datetime] = Field(default=None)
    failure_count: int = Field(default=0)
    cooldown_until: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
