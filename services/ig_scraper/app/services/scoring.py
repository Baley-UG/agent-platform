"""Deterministic post scoring (plan § 14b).

Six components, each normalised to [0, 1], combined as a weighted sum
and rescaled to [0, 100]:

    engagement_rate    — (likes + comments) / followers, clipped at 0.5
    velocity           — likes/hour during the first 24h after post,
                         normalised against 100 likes/hour
    view_efficiency    — (likes + comments) / play_count for video,
                         normalised against 0.10 ratio
    comment_intensity  — comments / likes, normalised against 0.05
    author_relative    — sigmoid of z-score vs author's last-30 median
                         engagement rate
    freshness          — exp(-age_days / IG_SCORE_HALFLIFE_DAYS)

All weights and thresholds are env-driven (`IG_SCORE_W_*`,
`IG_SCORE_HALFLIFE_DAYS`) so we can re-tune without a deploy.

`compute_score()` is the pure function — easy to unit-test with mocked
DB rows. `update_post_score()` and `recompute_recent_batch()` are the
side-effecting wrappers.
"""

import math
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import text
from sqlmodel import Session

from app.core.config import settings
from app.core.logging import logger

# Normalisation thresholds — what counts as "perfect" for each component.
# These are the upper clip points; anything beyond gets capped at 1.0.
_ENGAGEMENT_RATE_CAP = 0.5     # 50% of followers engaging = ceiling
_VELOCITY_CAP = 100.0          # 100 likes/hour during first 24h
_VIEW_EFFICIENCY_CAP = 0.10    # 10% engagement-on-views
_COMMENT_INTENSITY_CAP = 0.05  # 5% comments-to-likes


@dataclass
class ScoreResult:
    """Bundle: final score in [0, 100] + the per-component dict."""

    score: Decimal
    components: Dict[str, float]


def compute_score(
    *,
    like_count: int,
    comment_count: int,
    play_count: Optional[int],
    view_count: Optional[int],
    follower_count: Optional[int],
    taken_at: datetime,
    velocity_likes_per_hour: float,
    author_relative: float,
    now: Optional[datetime] = None,
) -> ScoreResult:
    """Pure score formula. Tests pass canned values; the wrappers below
    pull the inputs from the DB.

    `velocity_likes_per_hour` is the slope from `ig_post_metric_snapshots`;
    `author_relative` is the [0,1] sigmoid output already computed
    against the author's history. Both are computed by helpers below.
    """
    now = now or datetime.now(timezone.utc)

    # 1. engagement_rate — size-normalised baseline.
    follower = max(int(follower_count or 0), 1)
    er_raw = (like_count + comment_count) / follower
    engagement_rate = min(er_raw / _ENGAGEMENT_RATE_CAP, 1.0)

    # 2. velocity — pre-normalised input.
    velocity = max(0.0, min(velocity_likes_per_hour / _VELOCITY_CAP, 1.0))

    # 3. view_efficiency — only meaningful for video/reels.
    plays = play_count if play_count is not None else (view_count or 0)
    if plays and plays > 0:
        ve_raw = (like_count + comment_count) / plays
        view_efficiency = min(ve_raw / _VIEW_EFFICIENCY_CAP, 1.0)
    else:
        view_efficiency = 0.0  # photos contribute nothing here, by design

    # 4. comment_intensity — discussion-driving content.
    if like_count > 0:
        ci_raw = comment_count / like_count
        comment_intensity = min(ci_raw / _COMMENT_INTENSITY_CAP, 1.0)
    else:
        comment_intensity = 0.0

    # 5. author_relative — already a [0,1] sigmoid.
    author_relative = max(0.0, min(author_relative, 1.0))

    # 6. freshness — exponential decay.
    age_days = max(0.0, (now - taken_at).total_seconds() / 86400.0)
    freshness = math.exp(-age_days / max(settings.IG_SCORE_HALFLIFE_DAYS, 0.5))

    components = {
        "engagement_rate": engagement_rate,
        "velocity": velocity,
        "view_efficiency": view_efficiency,
        "comment_intensity": comment_intensity,
        "author_relative": author_relative,
        "freshness": freshness,
    }

    weighted = (
        settings.IG_SCORE_W_ENGAGEMENT * engagement_rate
        + settings.IG_SCORE_W_VELOCITY * velocity
        + settings.IG_SCORE_W_VIEW_EFFICIENCY * view_efficiency
        + settings.IG_SCORE_W_COMMENT_INTENSITY * comment_intensity
        + settings.IG_SCORE_W_AUTHOR_RELATIVE * author_relative
        + settings.IG_SCORE_W_FRESHNESS * freshness
    )
    score = Decimal(round(weighted * 100, 2))
    if score < 0:
        score = Decimal(0)
    if score > 100:
        score = Decimal(100)
    return ScoreResult(score=score, components=components)


# ----------------------------------------------------------------------
# Helper queries pulled out so they're cheap to mock in tests.
# ----------------------------------------------------------------------


def velocity_for_post(
    session: Session, post_id: int, taken_at: datetime
) -> float:
    """Estimate likes/hour over the first 24h.

    Uses the latest snapshot inside the 24h window (we don't need the
    full curve for v1 — just slope from t=0 to t=last-snapshot).
    """
    end = taken_at + timedelta(hours=24)
    row = session.execute(
        text(
            """
            SELECT scanned_at, like_count
            FROM ig_post_metric_snapshots
            WHERE post_id = :pid AND scanned_at <= :end
            ORDER BY scanned_at DESC
            LIMIT 1
            """
        ),
        {"pid": post_id, "end": end},
    ).first()
    if row is None:
        return 0.0
    last_scan, last_likes = row
    if last_scan.tzinfo is None:
        last_scan = last_scan.replace(tzinfo=timezone.utc)
    hours_elapsed = max((last_scan - taken_at).total_seconds() / 3600.0, 0.5)
    return float(last_likes) / hours_elapsed


def author_relative_score(
    session: Session, author_id: int, current_engagement: float
) -> float:
    """Sigmoid of z-score of `current_engagement` vs author's history.

    Returns 0.5 (neutral) when we have <3 posts to compare against —
    the score should not advantage or punish authors we just met.
    """
    rows = session.execute(
        text(
            """
            SELECT
                (p.like_count + p.comment_count)::float
                  / GREATEST(COALESCE(u.follower_count, 0), 1)
            FROM ig_posts p
            JOIN ig_users u ON u.id = p.author_id
            WHERE p.author_id = :aid
            ORDER BY p.taken_at DESC
            LIMIT 30
            """
        ),
        {"aid": author_id},
    ).scalars().all()
    history = [float(r) for r in rows if r is not None]
    if len(history) < 3:
        return 0.5

    median = statistics.median(history)
    if len(history) > 1:
        try:
            stdev = statistics.pstdev(history)
        except statistics.StatisticsError:
            stdev = 0.0
    else:
        stdev = 0.0
    if stdev <= 0.0:
        return 0.5

    z = (current_engagement - median) / stdev
    # Sigmoid clamped to [0,1]; |z|=2 → ~0.88.
    return 1.0 / (1.0 + math.exp(-z))


# ----------------------------------------------------------------------
# Side-effecting wrappers
# ----------------------------------------------------------------------


def update_post_score(session: Session, post_id: int) -> Optional[ScoreResult]:
    """Compute and persist the score for one post.

    Updates ig_posts.score / score_components / score_computed_at AND
    the most recent ig_post_metric_snapshots row's score column so
    historical analytics can plot score curves over time.

    Returns the result, or None if the post no longer exists (e.g.
    deleted between scrape and recompute).
    """
    row = session.execute(
        text(
            """
            SELECT
                p.like_count, p.comment_count, p.play_count, p.view_count,
                p.taken_at, p.author_id, u.follower_count
            FROM ig_posts p
            LEFT JOIN ig_users u ON u.id = p.author_id
            WHERE p.id = :pid
            """
        ),
        {"pid": post_id},
    ).first()
    if row is None:
        return None

    like_count, comment_count, play_count, view_count, taken_at, author_id, follower_count = row
    if taken_at.tzinfo is None:
        taken_at = taken_at.replace(tzinfo=timezone.utc)

    velocity_lph = velocity_for_post(session, post_id, taken_at)

    follower = max(int(follower_count or 0), 1)
    current_engagement = (like_count + comment_count) / follower
    rel = author_relative_score(session, author_id, current_engagement)

    result = compute_score(
        like_count=int(like_count),
        comment_count=int(comment_count),
        play_count=play_count,
        view_count=view_count,
        follower_count=follower_count,
        taken_at=taken_at,
        velocity_likes_per_hour=velocity_lph,
        author_relative=rel,
    )

    import json as _json

    now = datetime.now(timezone.utc)
    session.execute(
        text(
            """
            UPDATE ig_posts
            SET score             = :score,
                score_components  = CAST(:components AS jsonb),
                score_computed_at = :now
            WHERE id = :pid
            """
        ),
        {
            "score": result.score,
            "components": _json.dumps(result.components),
            "now": now,
            "pid": post_id,
        },
    )
    # Stamp the score onto the latest snapshot too.
    session.execute(
        text(
            """
            UPDATE ig_post_metric_snapshots
            SET score = :score
            WHERE post_id = :pid
              AND scanned_at = (
                  SELECT MAX(scanned_at) FROM ig_post_metric_snapshots WHERE post_id = :pid
              )
            """
        ),
        {"score": result.score, "pid": post_id},
    )
    return result


def recompute_recent_batch(session: Session, *, days: int = 30, batch_size: int = 500) -> int:
    """Nightly job — recompute scores for posts from the last `days` days.

    Returns the number of posts re-scored. Uses keyset pagination by
    `id` to avoid loading the whole window into memory.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    last_id = 0
    total = 0
    while True:
        rows = session.execute(
            text(
                """
                SELECT id FROM ig_posts
                WHERE taken_at >= :cutoff AND id > :last_id
                ORDER BY id ASC
                LIMIT :limit
                """
            ),
            {"cutoff": cutoff, "last_id": last_id, "limit": batch_size},
        ).scalars().all()
        if not rows:
            break
        for post_id in rows:
            update_post_score(session, int(post_id))
            total += 1
        session.commit()
        last_id = int(rows[-1])
        if len(rows) < batch_size:
            break
    logger.info("score_recompute_completed", scored=total, days=days)
    return total


# ----------------------------------------------------------------------
# Materialised view refresh
# ----------------------------------------------------------------------

REFRESHABLE_VIEWS: Tuple[str, ...] = (
    "ig_top_posts_by_author",
    "ig_author_posting_pattern",
    "ig_hashtag_velocity",
)


def refresh_views(session: Session, *, concurrently: bool = True) -> List[str]:
    """Refresh every materialised view. Returns the names that succeeded.

    `CONCURRENTLY` requires a unique index on the view (we created one
    in migration 0002) and avoids holding an exclusive lock during the
    refresh — important when readers are hitting these from MCP /
    REST in real time.
    """
    refreshed: List[str] = []
    for name in REFRESHABLE_VIEWS:
        try:
            session.execute(
                text(
                    f"REFRESH MATERIALIZED VIEW {'CONCURRENTLY ' if concurrently else ''}{name}"
                )
            )
            session.commit()
            refreshed.append(name)
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            logger.warning("mview_refresh_failed", view=name, error=str(exc))
    return refreshed
