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

    NOTE: the scraper's tables use `id` as the Instagram media pk (bigint
    PK), and `author_id` to reference `ig_users.id`. They do NOT have
    separate `pk`/`author_pk` columns — the bridge originally assumed
    that and silently returned None.

    Returns a dict keyed by our content_references columns plus a `media_urls`
    list so the caller can pick a download source.
    """
    sql = text(
        """
        SELECT
            p.id             AS source_external_id,
            p.code           AS shortcode,
            p.caption        AS caption,
            p.taken_at       AS taken_at,
            p.like_count, p.comment_count, p.play_count, p.view_count,
            p.media_type, p.product_type, p.media_urls, p.thumbnail_url,
            p.score,
            u.username
        FROM ig_posts p
        LEFT JOIN ig_users u ON u.id = p.author_id
        WHERE p.id = :pk
        LIMIT 1
        """
    )
    try:
        with read_session_scope() as session:
            # `id` is a BIGINT; cast the incoming string so the bind type
            # matches the column type and the index is used.
            row = session.exec(sql.bindparams(pk=int(post_pk))).mappings().first()
    except (ValueError, TypeError):
        logger.info("scraper_bridge_invalid_pk", post_pk=post_pk)
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("scraper_bridge_fetch_failed", post_pk=post_pk, error=str(exc))
        return None
    return dict(row) if row else None
