"""Project membership — links a user to a downstream project (content_pipeline).

`project_id` is a UUID (no FK) — content_pipeline owns the project rows.
We tolerate orphan memberships if a project is deleted there; the gateway
reads them to gate `/api/v1/cp/projects/{pid}/...` access.
"""

from datetime import datetime
from typing import Optional
from uuid import UUID

from sqlmodel import Field, SQLModel


PROJECT_ROLES = ("owner", "editor", "viewer")


class ProjectMembership(SQLModel, table=True):
    """One row per (user, project_id) pair."""

    __tablename__ = "project_membership"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    project_id: UUID = Field(index=True)
    role: str = Field(default="editor")
    created_at: datetime = Field(default_factory=datetime.utcnow)
