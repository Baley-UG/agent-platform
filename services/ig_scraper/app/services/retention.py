"""GDPR retention helpers (plan § 14c item 9).

The schema already has `text_expires_at` on `ig_comments` and
`biography_expires_at` on `ig_users`. M9 plumbs the policy:

- `apply_default_ttl(session)` — for rows whose expiry is null but
  whose data is older than `IG_COMMENT_TTL_DAYS` / `IG_BIOGRAPHY_TTL_DAYS`,
  set the TTL. Cheap and idempotent.
- `nullify_expired(session)` — for rows whose TTL has elapsed, NULL
  the free-text fields. The `raw` JSONB and structural columns
  remain so analytics still work.

Both are gated by `IG_RETENTION_ENABLED` (default false). Operators
turn it on when a takedown / DPA-style policy is in force; it never
runs by default.
"""

from datetime import datetime, timedelta, timezone
from typing import Dict

from sqlalchemy import text
from sqlmodel import Session

from app.core.config import settings
from app.core.logging import logger


def apply_default_ttl(session: Session) -> Dict[str, int]:
    """Stamp `*_expires_at` columns based on the configured TTL.

    Returns counts per table. Skips entirely when the corresponding
    `IG_*_TTL_DAYS` is 0 (i.e. unset).
    """
    if not settings.IG_RETENTION_ENABLED:
        return {"comments": 0, "biographies": 0}

    counts = {"comments": 0, "biographies": 0}
    if settings.IG_COMMENT_TTL_DAYS > 0:
        result = session.execute(
            text(
                """
                UPDATE ig_comments
                SET text_expires_at = captured_at + (:days || ' days')::interval
                WHERE text_expires_at IS NULL AND text IS NOT NULL
                """
            ),
            {"days": settings.IG_COMMENT_TTL_DAYS},
        )
        counts["comments"] = result.rowcount or 0
    if settings.IG_BIOGRAPHY_TTL_DAYS > 0:
        result = session.execute(
            text(
                """
                UPDATE ig_users
                SET biography_expires_at = last_seen_at + (:days || ' days')::interval
                WHERE biography_expires_at IS NULL AND biography IS NOT NULL
                """
            ),
            {"days": settings.IG_BIOGRAPHY_TTL_DAYS},
        )
        counts["biographies"] = result.rowcount or 0
    return counts


def nullify_expired(session: Session) -> Dict[str, int]:
    """NULL out free-text columns whose TTL has elapsed.

    `raw` JSONB and all engagement counters stay; only the human-
    readable strings disappear. That's enough to satisfy "we no longer
    store the natural-language content" requests while preserving
    analytical value.
    """
    if not settings.IG_RETENTION_ENABLED:
        return {"comments": 0, "biographies": 0}

    counts = {"comments": 0, "biographies": 0}
    now = datetime.now(timezone.utc)

    result = session.execute(
        text(
            "UPDATE ig_comments SET text = NULL "
            "WHERE text IS NOT NULL "
            "  AND text_expires_at IS NOT NULL "
            "  AND text_expires_at <= :now"
        ),
        {"now": now},
    )
    counts["comments"] = result.rowcount or 0

    result = session.execute(
        text(
            "UPDATE ig_users SET biography = NULL "
            "WHERE biography IS NOT NULL "
            "  AND biography_expires_at IS NOT NULL "
            "  AND biography_expires_at <= :now"
        ),
        {"now": now},
    )
    counts["biographies"] = result.rowcount or 0

    if any(counts.values()):
        logger.info("retention_nullified", **counts)
    return counts
