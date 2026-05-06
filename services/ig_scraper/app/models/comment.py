"""ig_comments."""

from datetime import datetime
from typing import Optional

from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Column, Field, SQLModel

from app.models.base import utcnow


class Comment(SQLModel, table=True):
    """A comment on a Post.

    `comment_text_expires_at` is the GDPR TTL hook — a nightly job (M9,
    disabled by default) can null out `text` past its expiry.
    """

    __tablename__ = "ig_comments"

    id: int = Field(primary_key=True)
    post_id: int = Field(foreign_key="ig_posts.id", index=True)
    author_id: int = Field(foreign_key="ig_users.id", index=True)
    parent_comment_id: Optional[int] = Field(default=None)
    text: Optional[str] = Field(default=None)
    like_count: int = Field(default=0)
    created_at_ig: Optional[datetime] = Field(default=None)
    raw: Optional[dict] = Field(default=None, sa_column=Column(JSONB, nullable=True))
    captured_at: datetime = Field(default_factory=utcnow)
    text_expires_at: Optional[datetime] = Field(default=None)
