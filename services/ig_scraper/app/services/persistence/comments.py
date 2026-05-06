"""Upsert helper for ig_comments + their authors."""

import json
from datetime import datetime, timezone
from typing import Any, Dict, Iterable

from sqlalchemy import text
from sqlmodel import Session

from app.services.persistence.users import upsert_ig_user

_UPSERT = text(
    """
    INSERT INTO ig_comments (
        id, post_id, author_id, parent_comment_id, text,
        like_count, created_at_ig, raw, captured_at
    )
    VALUES (
        :id, :post_id, :author_id, :parent_comment_id, :text,
        :like_count, :created_at_ig, CAST(:raw AS jsonb), :now
    )
    ON CONFLICT (id) DO UPDATE SET
        text          = COALESCE(EXCLUDED.text, ig_comments.text),
        like_count    = EXCLUDED.like_count,
        captured_at   = EXCLUDED.captured_at
    """
)


def upsert_comments(
    session: Session, post_id: int, comments: Iterable[Dict[str, Any]]
) -> int:
    """Upsert a sequence of comments. Returns the count saved.

    Each comment must expose {id, user, text, like_count}. The author
    block is upserted into `ig_users` first so the FK is satisfied.
    """
    now = datetime.now(timezone.utc)
    saved = 0
    for comment in comments:
        if "id" not in comment:
            continue
        user = comment.get("user") or {}
        if "id" not in user:
            # Comments without an author payload are useless — skip.
            continue
        author_id = upsert_ig_user(session, user)

        created_at_ig = comment.get("created_at")
        if isinstance(created_at_ig, (int, float)):
            created_at_ig = datetime.fromtimestamp(created_at_ig, tz=timezone.utc)
        if not isinstance(created_at_ig, datetime):
            created_at_ig = None

        session.execute(
            _UPSERT,
            {
                "id": int(comment["id"]),
                "post_id": post_id,
                "author_id": author_id,
                "parent_comment_id": comment.get("parent_comment_id"),
                "text": comment.get("text"),
                "like_count": int(comment.get("comment_like_count") or comment.get("like_count") or 0),
                "created_at_ig": created_at_ig,
                "raw": json.dumps(comment, default=str),
                "now": now,
            },
        )
        saved += 1
    return saved
