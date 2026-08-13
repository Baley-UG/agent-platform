"""Schemas for the admin panel auth endpoints (docs: admin UI backend-api.md § 3.1)."""

from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, EmailStr, Field, SecretStr


class AdminMembership(BaseModel):
    """Per-project membership entry (placeholder until projects land)."""

    id: int
    project_id: str
    role: Literal["owner", "editor", "viewer"]


class AdminUserRead(BaseModel):
    """User payload consumed by the admin panel."""

    id: int
    email: str
    name: Optional[str] = None
    role: Literal["admin", "member"] = "admin"
    status: Literal["active", "disabled"] = "active"
    last_login_at: Optional[datetime] = None
    created_at: datetime
    memberships: List[AdminMembership] = Field(default_factory=list)


class AdminTokenResponse(BaseModel):
    """Access/refresh pair plus the user, returned by login/refresh/oidc."""

    access_token: str
    refresh_token: str
    expires_at: datetime
    user: AdminUserRead


class AdminLoginRequest(BaseModel):
    """JSON body for POST /admin/auth/login."""

    email: EmailStr
    password: SecretStr


class AdminRefreshRequest(BaseModel):
    """JSON body for POST /admin/auth/refresh and /logout."""

    refresh_token: str


class AdminChangePasswordRequest(BaseModel):
    """JSON body for POST /admin/auth/change-password."""

    current_password: SecretStr
    new_password: SecretStr
