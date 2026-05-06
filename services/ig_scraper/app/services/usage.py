"""Per-account/day usage counter increments.

Worker calls `increment(...)` after every instagrapi call (calls), every
post upsert (posts), every comment upsert (comments), every story
insert (stories). The row is upserted on (date, account_id) so the
first call of the day creates it.

We use a single SQL statement with `ON CONFLICT ... DO UPDATE SET
calls_made = ig_usage_daily.calls_made + EXCLUDED.calls_made` so two
workers can increment the same row concurrently without losing writes.
"""

import uuid

from sqlalchemy import text
from sqlmodel import Session

from app.core.logging import logger

_UPSERT = text(
    """
    INSERT INTO ig_usage_daily
        (date, account_id, calls_made, posts_saved, comments_saved,
         stories_saved, proxy_bytes, challenge_count)
    VALUES
        (CURRENT_DATE, :account_id, :calls, :posts, :comments,
         :stories, :proxy_bytes, :challenges)
    ON CONFLICT (date, account_id) DO UPDATE SET
        calls_made      = ig_usage_daily.calls_made      + EXCLUDED.calls_made,
        posts_saved     = ig_usage_daily.posts_saved     + EXCLUDED.posts_saved,
        comments_saved  = ig_usage_daily.comments_saved  + EXCLUDED.comments_saved,
        stories_saved   = ig_usage_daily.stories_saved   + EXCLUDED.stories_saved,
        proxy_bytes     = ig_usage_daily.proxy_bytes     + EXCLUDED.proxy_bytes,
        challenge_count = ig_usage_daily.challenge_count + EXCLUDED.challenge_count
    """
)


def increment(
    session: Session,
    account_id: uuid.UUID,
    *,
    calls: int = 0,
    posts: int = 0,
    comments: int = 0,
    stories: int = 0,
    proxy_bytes: int = 0,
    challenges: int = 0,
) -> None:
    """Atomically bump today's counters for `account_id`.

    Caller is responsible for the surrounding transaction (we don't
    commit inside this function — usage updates piggy-back on whatever
    transaction the worker already has open).
    """
    if not any((calls, posts, comments, stories, proxy_bytes, challenges)):
        return
    try:
        session.execute(
            _UPSERT,
            {
                "account_id": account_id,
                "calls": calls,
                "posts": posts,
                "comments": comments,
                "stories": stories,
                "proxy_bytes": proxy_bytes,
                "challenges": challenges,
            },
        )
    except Exception as exc:  # noqa: BLE001
        # Don't let a counter blip kill the scrape — log and move on.
        logger.warning(
            "usage_increment_failed",
            account_id=str(account_id),
            error=str(exc),
        )
