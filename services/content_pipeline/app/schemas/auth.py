"""Auth + user schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, EmailStr, Field

GlobalRole = Literal["admin", "member"]
ProjectRole = Literal["owner", "editor", "viewer"]
UserStatus = Literal["active", "disabled"]


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    expires_at: datetime
    token_type: Literal["Bearer"] = "Bearer"
    user: "UserRead"


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1)
    new_password: str = Field(min_length=8, max_length=200)


class ProjectMembershipRead(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    role: str

    model_config = {"from_attributes": True}


class UserRead(BaseModel):
    id: uuid.UUID
    email: str
    name: Optional[str]
    role: str
    status: str
    last_login_at: Optional[datetime]
    created_at: datetime
    memberships: List[ProjectMembershipRead] = []

    model_config = {"from_attributes": True}


class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=200)
    name: Optional[str] = None
    role: GlobalRole = "member"


class UserUpdate(BaseModel):
    name: Optional[str] = None
    role: Optional[GlobalRole] = None
    status: Optional[UserStatus] = None
    password: Optional[str] = Field(default=None, min_length=8, max_length=200)


class MembershipCreate(BaseModel):
    user_id: uuid.UUID
    role: ProjectRole = "editor"


class MembershipUpdate(BaseModel):
    role: ProjectRole


# Pydantic v2 forward-ref resolution
TokenResponse.model_rebuild()
