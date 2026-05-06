"""Pydantic schemas for /api/v1/proxies."""

import uuid
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

ProxyProtocol = Literal["http", "https", "socks5"]


class ProxyCreate(BaseModel):
    """Body for POST /proxies."""

    protocol: ProxyProtocol = "http"
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(ge=1, le=65535)
    username: Optional[str] = None
    password: Optional[str] = Field(default=None, repr=False, description="Encrypted server-side.")
    label: Optional[str] = Field(default=None, description="Provider tag, e.g. 'brightdata-resi-dk'.")


class ProxyUpdate(BaseModel):
    """Body for PATCH /proxies/{id}."""

    protocol: Optional[ProxyProtocol] = None
    host: Optional[str] = None
    port: Optional[int] = Field(default=None, ge=1, le=65535)
    username: Optional[str] = None
    password: Optional[str] = Field(default=None, repr=False)
    label: Optional[str] = None
    status: Optional[str] = None


class ProxyRead(BaseModel):
    """Read shape — never includes the decrypted password."""

    id: uuid.UUID
    protocol: str
    host: str
    port: int
    username: Optional[str]
    label: Optional[str]
    status: str
    has_password: bool
    last_ok_at: Optional[datetime]
    failure_count: int
    cooldown_until: Optional[datetime]
    created_at: datetime
    updated_at: datetime


class ProxyTestResponse(BaseModel):
    """Result of POST /proxies/{id}/test."""

    id: uuid.UUID
    ok: bool
    latency_ms: Optional[int]
    status_code: Optional[int]
    public_ip: Optional[str] = Field(default=None, description="IP seen by the test target — useful for residential rotations.")
    error: Optional[str] = None
