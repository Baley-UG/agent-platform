"""Response schemas for /users — Instagram profiles we've scraped."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_serializer


class IgUserSummary(BaseModel):
    """Slim row for list responses."""

    id: int = Field(description="Instagram user pk; serialized as string (bigint-safe).")

    @field_serializer("id")
    def _ser_id(self, v: int) -> str:
        return str(v)

    username: str
    full_name: Optional[str] = None
    biography: Optional[str] = None
    follower_count: Optional[int] = None
    following_count: Optional[int] = None
    media_count: Optional[int] = None
    is_business: Optional[bool] = None
    is_verified: Optional[bool] = None
    is_private: Optional[bool] = None
    profile_pic_url: Optional[str] = None
    first_seen_at: Optional[datetime] = None
    last_seen_at: Optional[datetime] = None

    model_config = ConfigDict(extra="allow")


class IgUserStats(BaseModel):
    """Aggregate counters derived from `ig_posts` joined to this author."""

    posts_in_db: int = Field(description="How many of this user's posts we've persisted.")
    avg_likes: Optional[float] = None
    avg_play_count: Optional[float] = None
    avg_score: Optional[float] = None
    max_score: Optional[float] = None
    last_post_at: Optional[datetime] = None


class IgUserDetail(IgUserSummary):
    """Profile + aggregate stats + raw HikerAPI payload."""

    raw: Optional[Dict[str, Any]] = None
    stats: IgUserStats
