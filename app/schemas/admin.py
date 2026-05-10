"""Admin-panel schemas — auth, users, memberships."""

from __future__ import annotations

from datetime import datetime
from typing import List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field

GlobalRole = Literal["admin", "member"]
ProjectRole = Literal["owner", "editor", "viewer"]
UserStatus = Literal["active", "disabled"]


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1)


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1)
    new_password: str = Field(min_length=8, max_length=200)


class ProjectMembershipRead(BaseModel):
    id: int
    project_id: UUID
    role: str

    model_config = {"from_attributes": True}


class AdminUserRead(BaseModel):
    id: int
    email: str
    name: Optional[str]
    role: str
    status: str
    last_login_at: Optional[datetime]
    created_at: datetime
    memberships: List[ProjectMembershipRead] = []

    model_config = {"from_attributes": True}


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    expires_at: datetime
    token_type: Literal["Bearer"] = "Bearer"
    user: AdminUserRead


class UserCreateRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=200)
    name: Optional[str] = None
    role: GlobalRole = "member"


class UserUpdateRequest(BaseModel):
    name: Optional[str] = None
    role: Optional[GlobalRole] = None
    status: Optional[UserStatus] = None
    password: Optional[str] = Field(default=None, min_length=8, max_length=200)


class MembershipCreateRequest(BaseModel):
    user_id: int
    role: ProjectRole = "editor"


class MembershipUpdateRequest(BaseModel):
    role: ProjectRole
