"""Response schemas for the /posts API.

The detail endpoint is generous on purpose — admins / pipelines want
everything in one call. List entries are a thinner subset of the same
columns so a typical "browse" page stays under a few KB per row.

`raw` is a free-form JSONB of the original HikerAPI payload; we don't
type its fields, just expose it. `model_config={'extra': 'allow'}` keeps
forward-compat if the scraper adds columns we haven't surfaced yet.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Union
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_serializer


# Instagram media pks and user ids are 19-digit integers, larger than
# `Number.MAX_SAFE_INTEGER` (2^53 - 1 = 16 digits) in Javascript.
# Returning them as JSON numbers silently corrupts the last 3-4 digits
# on the client. We keep them as Python `int` for ergonomic backend use,
# but serialize to `str` on the wire so clients can roundtrip safely.
def _bigint_to_str(value: int) -> str:
    return str(value)


class PostScoreComponents(BaseModel):
    """Component scores (each in [0,1]) that compose the final 0-100 `score`."""

    engagement_rate: Optional[float] = None
    velocity: Optional[float] = None
    view_efficiency: Optional[float] = None
    comment_intensity: Optional[float] = None
    author_relative: Optional[float] = None
    freshness: Optional[float] = None

    model_config = ConfigDict(extra="allow")


class PostAuthorSummary(BaseModel):
    """Author fields embedded inline on list responses (cheaper than a join)."""

    author_username: str
    author_full_name: Optional[str] = None
    author_is_verified: Optional[bool] = None
    author_follower_count: Optional[int] = None
    author_profile_pic_url: Optional[str] = None

    model_config = ConfigDict(extra="allow")


class PostListItem(PostAuthorSummary):
    """Slim post row returned by `GET /posts` (list).

    Use `GET /posts/{id}` for the full detail view including the author
    profile, score components, and `raw` HikerAPI payload.
    """

    id: int = Field(description="Instagram media pk (bigint; serialized as string).")

    @field_serializer("id")
    def _ser_id(self, v: int) -> str:
        return _bigint_to_str(v)
    code: str = Field(description="Instagram shortcode (e.g. `DYFLUrIkYku`).")
    media_type: int = Field(description="1=photo, 2=video, 8=carousel.")
    product_type: Optional[str] = None
    taken_at: datetime

    like_count: int
    comment_count: int
    play_count: Optional[int] = None
    view_count: Optional[int] = None
    video_duration: Optional[float] = None

    caption: Optional[str] = None
    caption_length: Optional[int] = None
    language: Optional[str] = None
    hashtags: Optional[List[str]] = None
    mentions: Optional[List[str]] = None

    thumbnail_url: Optional[str] = None
    media_urls: Optional[List[str]] = None

    score: Optional[Decimal] = Field(default=None, description="0-100 composite score.")


class PostCommentItem(BaseModel):
    """Single comment, used both inline and on the dedicated comments endpoint."""

    id: int
    text: Optional[str] = None
    like_count: Optional[int] = None
    created_at_ig: Optional[datetime] = None
    author_username: Optional[str] = None

    @field_serializer("id")
    def _ser_id(self, v: int) -> str:
        return _bigint_to_str(v)

    model_config = ConfigDict(extra="allow")


class PostDetail(BaseModel):
    """Full single-post view returned by `GET /posts/{post_id}`.

    Includes every column on `ig_posts`, the author's profile fields,
    parsed caption features, media URLs, score components, and
    optionally the top-N comments inline.
    """

    # ---- core ----
    id: int
    code: str
    media_type: int
    product_type: Optional[str] = None
    taken_at: datetime
    video_duration: Optional[float] = None

    # ---- metrics ----
    like_count: int
    comment_count: int
    play_count: Optional[int] = None
    view_count: Optional[int] = None
    save_count: Optional[int] = None

    # ---- caption analysis ----
    caption: Optional[str] = None
    caption_length: Optional[int] = None
    language: Optional[str] = None
    emoji_count: Optional[int] = None
    hashtag_count: Optional[int] = None
    mention_count: Optional[int] = None
    has_question: Optional[bool] = None
    has_cta: Optional[bool] = None
    caption_simhash: Optional[int] = None

    # ---- tags ----
    hashtags: Optional[List[str]] = None
    mentions: Optional[List[str]] = None

    # ---- media ----
    thumbnail_url: Optional[str] = None
    media_urls: Optional[List[str]] = None
    location: Optional[Dict[str, Any]] = None
    music_info: Optional[Dict[str, Any]] = None
    audio_track_id: Optional[str] = None

    # ---- scoring ----
    score: Optional[Decimal] = None
    score_components: Optional[PostScoreComponents] = None
    score_computed_at: Optional[datetime] = None

    # ---- provenance ----
    first_seen_at: datetime
    last_seen_at: datetime
    discovered_via_job_id: Optional[UUID] = None

    # ---- raw HikerAPI payload (untyped) ----
    raw: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Original HikerAPI media object, kept verbatim for re-derivation.",
    )

    # ---- author profile (flat, inlined from ig_users join) ----
    author_id: int
    author_username: str

    @field_serializer("id", "author_id", "caption_simhash")
    def _ser_bigints(self, v):
        # caption_simhash is a 64-bit signed int → can also overflow JS.
        return None if v is None else _bigint_to_str(v)

    author_full_name: Optional[str] = None
    author_biography: Optional[str] = None
    follower_count: Optional[int] = None
    following_count: Optional[int] = None
    media_count: Optional[int] = None
    is_business: Optional[bool] = None
    is_verified: Optional[bool] = None
    is_private: Optional[bool] = None
    profile_pic_url: Optional[str] = None

    # ---- inline comments (only when ?include_comments=N > 0) ----
    comments: List[PostCommentItem] = Field(default_factory=list)

    model_config = ConfigDict(extra="allow")
