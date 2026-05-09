"""Shared SQLModel helpers.

All content_pipeline tables live in the `content_pipeline` Postgres schema.
"""

from datetime import datetime, timezone

SCHEMA_NAME = "content_pipeline"


def utcnow() -> datetime:
    """Return a timezone-aware UTC `datetime.now()`."""
    return datetime.now(timezone.utc)
