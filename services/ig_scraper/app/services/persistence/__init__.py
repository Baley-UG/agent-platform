"""Persistence helpers for the scraping pipeline.

Each module is a thin wrapper around `INSERT ... ON CONFLICT DO UPDATE`
patterns. Idempotent on IG primary keys so re-running a scrape is
always safe.

Scrapers compose these helpers; they don't know SQL directly.
"""

from app.services.persistence.audio import upsert_audio_track
from app.services.persistence.comments import upsert_comments
from app.services.persistence.hashtags import upsert_hashtag
from app.services.persistence.highlights import link_highlight_item, upsert_highlight
from app.services.persistence.posts import upsert_post
from app.services.persistence.stories import insert_story
from app.services.persistence.users import upsert_ig_user

__all__ = [
    "insert_story",
    "link_highlight_item",
    "upsert_audio_track",
    "upsert_comments",
    "upsert_hashtag",
    "upsert_highlight",
    "upsert_post",
    "upsert_ig_user",
]
