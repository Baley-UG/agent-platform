"""Shared model helpers."""

import uuid
from datetime import datetime, timezone


def utcnow() -> datetime:
    """Return current UTC datetime — used as default_factory for timestamp columns."""
    return datetime.now(timezone.utc)


def new_uuid() -> uuid.UUID:
    """Return a fresh UUID v4 — used as default_factory for UUID PKs."""
    return uuid.uuid4()
