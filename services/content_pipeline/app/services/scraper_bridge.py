"""Read-only access to the `ig_scraper` schema.

We share a Postgres instance with `ig_scraper` (`ig_*` tables in the public
schema). Rather than depending on the scraper's SQLModel classes — which
would couple our deploy lifecycle to theirs — we issue raw SELECTs through
the read engine and return plain dicts.

`fetch_ig_post(post_pk)` returns the columns we need to spawn a
`content_references` row. Returns None if the post isn't in the scraper's
DB (the admin maybe pasted a wrong id).
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import text

from app.core.logging import logger
from app.services.database import read_session_scope


def fetch_ig_post(post_pk: str) -> Optional[dict]:
    """Pull the columns we need from `ig_scraper.ig_posts` (lives in public schema).

    Returns a dict keyed by our content_references columns plus a `media_urls`
    list so the caller can pick a download source.
    """
    sql = text(
        """
        SELECT
            p.pk             AS source_external_id,
            p.code           AS shortcode,
            p.caption_text   AS caption,
            p.taken_at       AS taken_at,
            p.like_count, p.comment_count, p.play_count, p.view_count,
            p.media_type, p.product_type, p.media_urls, p.thumbnail_url,
            p.score,
            u.username
        FROM ig_posts p
        LEFT JOIN ig_users u ON u.pk = p.author_pk
        WHERE p.pk = :pk
        LIMIT 1
        """
    )
    try:
        with read_session_scope() as session:
            row = session.exec(sql.bindparams(pk=post_pk)).mappings().first()
    except Exception as exc:  # noqa: BLE001
        logger.warning("scraper_bridge_fetch_failed", post_pk=post_pk, error=str(exc))
        return None
    return dict(row) if row else None
