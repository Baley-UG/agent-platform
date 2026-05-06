"""Pydantic request/response schemas for /api/v1/accounts.

The `Read` schema deliberately omits `password_enc` and `session_blob`
so they can never leak through the API. The login response surface is
narrow on purpose — we tell the caller the new status and timestamps,
not the underlying instagrapi payload.
"""

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class AccountCreate(BaseModel):
    """Body for POST /accounts.

    `password` is the plaintext password the operator types in. The
    service encrypts it with the IG_SECRET_KEY before storage and never
    logs it.
    """

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, repr=False, description="Plaintext IG password — encrypted server-side.")
    proxy_id: Optional[uuid.UUID] = None
    role: str = Field(default="scraper", description="'scraper' (default) or 'canary'.")
    timezone: str = Field(default="UTC")
    active_hours_start: int = Field(default=8, ge=0, le=23)
    active_hours_end: int = Field(default=23, ge=1, le=24)
    weekday_pattern: int = Field(default=127, ge=1, le=127)
    notes: Optional[str] = None


class AccountUpdate(BaseModel):
    """Body for PATCH /accounts/{id}. All fields optional."""

    password: Optional[str] = Field(default=None, repr=False, description="Re-encrypts on save when set.")
    proxy_id: Optional[uuid.UUID] = None
    role: Optional[str] = None
    status: Optional[str] = None
    quota_tier: Optional[str] = None
    timezone: Optional[str] = None
    active_hours_start: Optional[int] = Field(default=None, ge=0, le=23)
    active_hours_end: Optional[int] = Field(default=None, ge=1, le=24)
    weekday_pattern: Optional[int] = Field(default=None, ge=1, le=127)
    notes: Optional[str] = None


class AccountRead(BaseModel):
    """Response shape for GET /accounts and friends.

    Never carries password_enc or session_blob.
    """

    id: uuid.UUID
    username: str
    status: str
    role: str
    proxy_id: Optional[uuid.UUID]
    timezone: str
    active_hours_start: int
    active_hours_end: int
    weekday_pattern: int
    quota_tier: str
    cooldown_until: Optional[datetime]
    last_used_at: Optional[datetime]
    last_login_at: Optional[datetime]
    failure_count: int
    notes: Optional[str]
    has_session: bool = Field(description="True if session_blob is populated.")
    created_at: datetime
    updated_at: datetime


class AccountLoginRequest(BaseModel):
    """Body for POST /accounts/{id}/login.

    For 2FA accounts, pass `verification_code`. instagrapi will accept it
    if a challenge is in flight.
    """

    verification_code: Optional[str] = Field(
        default=None, description="2FA / SMS / email code if instagrapi is in challenge state."
    )


class AccountLoginResponse(BaseModel):
    """Result of a login attempt — status drives next action."""

    id: uuid.UUID
    status: str
    last_login_at: Optional[datetime]
    has_session: bool
    detail: Optional[str] = Field(default=None, description="Human-readable note on the outcome.")
