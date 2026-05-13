"""Project membership — links a user to a project.

`projects` lives in `public` (platform-wide multi-tenancy root); we hold
a real FK on `project_id` with `ON DELETE CASCADE` so deleting a project
removes all its memberships in one shot — no orphan rows.
"""

from datetime import datetime
from typing import Optional
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlmodel import Field, SQLModel


PROJECT_ROLES = ("owner", "editor", "viewer")


class ProjectMembership(SQLModel, table=True):
    """One row per (user, project_id) pair."""

    __tablename__ = "project_membership"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    # FK → public.projects.id (CASCADE on delete). content_pipeline.* tables
    # share the same `public.projects` parent.
    project_id: UUID = Field(
        sa_column=sa.Column(
            PGUUID(as_uuid=True),
            sa.ForeignKey("public.projects.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )
    role: str = Field(default="editor")
    created_at: datetime = Field(default_factory=datetime.utcnow)
