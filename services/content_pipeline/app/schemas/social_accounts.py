"""Social account schemas (publishing accounts on IG/TikTok)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

Provider = Literal["instagram", "tiktok"]
Status = Literal["active", "expired", "revoked", "pending_oauth"]


class SocialAccountCreate(BaseModel):
    provider: Provider
    handle: str = Field(min_length=1, max_length=255)
    external_account_id: Optional[str] = None
    # Plain credentials in (will be encrypted at rest); typically the admin
    # uploads OAuth tokens through a flow that lands here. Treat with care.
    credentials: Optional[dict] = None


class SocialAccountUpdate(BaseModel):
    handle: Optional[str] = None
    external_account_id: Optional[str] = None
    credentials: Optional[dict] = None
    status: Optional[Status] = None


class SocialAccountRead(BaseModel):
    """Public read shape — credentials are NEVER returned."""

    id: uuid.UUID
    project_id: uuid.UUID
    provider: str
    handle: str
    external_account_id: Optional[str]
    status: str
    last_used_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime
    has_credentials: bool

    model_config = {"from_attributes": True}
