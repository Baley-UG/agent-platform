"""ig_posts, ig_post_hashtags, ig_post_metric_snapshots."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import ARRAY, Column, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from app.models.base import utcnow


class Post(SQLModel, table=True):
    """A feed post or reel.

    Reels and feed posts share this table — they have the same shape on
    the wire. `media_type` and `product_type` distinguish the variants.
    """

    __tablename__ = "ig_posts"

    id: int = Field(primary_key=True, description="IG numeric pk.")
    code: str = Field(unique=True, index=True, description="IG shortcode (/p/<code>/).")
    media_type: int = Field(description="1 photo, 2 video, 8 carousel.")
    product_type: Optional[str] = Field(default=None, description="feed | clips | igtv")
    author_id: int = Field(foreign_key="ig_users.id", index=True)

    caption: Optional[str] = Field(default=None)
    taken_at: datetime = Field(index=True)
    like_count: int = Field(default=0, index=True)
    comment_count: int = Field(default=0)
    play_count: Optional[int] = Field(default=None, index=True)
    view_count: Optional[int] = Field(default=None)
    save_count: Optional[int] = Field(default=None)
    video_duration: Optional[float] = Field(default=None)
    thumbnail_url: Optional[str] = Field(default=None)
    media_urls: Optional[list] = Field(default=None, sa_column=Column(JSONB, nullable=True))
    location: Optional[dict] = Field(default=None, sa_column=Column(JSONB, nullable=True))
    music_info: Optional[dict] = Field(default=None, sa_column=Column(JSONB, nullable=True))
    audio_track_id: Optional[str] = Field(default=None, foreign_key="ig_audio_tracks.id")

    hashtags: Optional[list[str]] = Field(default=None, sa_column=Column(ARRAY(String), nullable=True))
    mentions: Optional[list[str]] = Field(default=None, sa_column=Column(ARRAY(String), nullable=True))

    # Caption features (M4 will populate; M1 just declares the columns).
    language: Optional[str] = Field(default=None)
    emoji_count: Optional[int] = Field(default=None)
    hashtag_count: Optional[int] = Field(default=None)
    mention_count: Optional[int] = Field(default=None)
    caption_length: Optional[int] = Field(default=None)
    has_question: Optional[bool] = Field(default=None)
    has_cta: Optional[bool] = Field(default=None)
    caption_simhash: Optional[int] = Field(default=None, index=True)

    # Score (M8 populates).
    score: Optional[Decimal] = Field(default=None, max_digits=5, decimal_places=2, index=True)
    score_components: Optional[dict] = Field(default=None, sa_column=Column(JSONB, nullable=True))
    score_computed_at: Optional[datetime] = Field(default=None)

    raw: Optional[dict] = Field(default=None, sa_column=Column(JSONB, nullable=True))
    discovered_via_job_id: Optional[uuid.UUID] = Field(default=None, foreign_key="ig_scrape_jobs.id")
    first_seen_at: datetime = Field(default_factory=utcnow)
    last_seen_at: datetime = Field(default_factory=utcnow)


class PostHashtag(SQLModel, table=True):
    """Many-to-many between posts and hashtags."""

    __tablename__ = "ig_post_hashtags"

    post_id: int = Field(foreign_key="ig_posts.id", primary_key=True)
    hashtag: str = Field(foreign_key="ig_hashtags.name", primary_key=True, index=True)


class PostMetricSnapshot(SQLModel, table=True):
    """Append-only engagement snapshot — one row per scan of a post.

    Required for the velocity component of the score, and gives us
    engagement-curve analytics for free.
    """

    __tablename__ = "ig_post_metric_snapshots"

    post_id: int = Field(foreign_key="ig_posts.id", primary_key=True)
    scanned_at: datetime = Field(default_factory=utcnow, primary_key=True)
    like_count: int
    comment_count: int
    play_count: Optional[int] = Field(default=None)
    view_count: Optional[int] = Field(default=None)
    save_count: Optional[int] = Field(default=None)
    score: Optional[Decimal] = Field(default=None, max_digits=5, decimal_places=2)
