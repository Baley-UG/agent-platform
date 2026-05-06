"""Materialised analytical views for M8 scoring.

Three views power the most common dashboards / MCP queries:

- `ig_top_posts_by_author`: per-author ranked posts by score.
- `ig_author_posting_pattern`: when each author posts and what scores
  those slots earn.
- `ig_hashtag_velocity`: 7-day-vs-prior-7-day post counts and average
  scores per hashtag — the trend-detector.

Each view has a unique index so REFRESH MATERIALIZED VIEW CONCURRENTLY
can run while readers query them. The scheduler hits these once a day.

Revision ID: 0002_scoring_views
Revises: 0001_initial_phase1
Create Date: 2026-05-06

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0002_scoring_views"
down_revision: Union[str, Sequence[str], None] = "0001_initial_phase1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE MATERIALIZED VIEW ig_top_posts_by_author AS
        SELECT
            author_id,
            id AS post_id,
            score,
            taken_at,
            ROW_NUMBER() OVER (
                PARTITION BY author_id ORDER BY score DESC NULLS LAST, taken_at DESC
            ) AS rank
        FROM ig_posts
        WHERE score IS NOT NULL
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX ig_top_posts_by_author_pk "
        "ON ig_top_posts_by_author (author_id, post_id)"
    )

    op.execute(
        """
        CREATE MATERIALIZED VIEW ig_author_posting_pattern AS
        SELECT
            author_id,
            EXTRACT(hour FROM taken_at AT TIME ZONE 'UTC')::int AS hour_of_day,
            EXTRACT(dow  FROM taken_at AT TIME ZONE 'UTC')::int AS weekday,
            COUNT(*) AS post_count,
            AVG(score) AS avg_score
        FROM ig_posts
        WHERE score IS NOT NULL
        GROUP BY author_id, hour_of_day, weekday
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX ig_author_posting_pattern_pk "
        "ON ig_author_posting_pattern (author_id, hour_of_day, weekday)"
    )

    op.execute(
        """
        CREATE MATERIALIZED VIEW ig_hashtag_velocity AS
        WITH last_week AS (
            SELECT ph.hashtag,
                   COUNT(*) AS post_count,
                   AVG(p.score) AS avg_score
            FROM ig_post_hashtags ph
            JOIN ig_posts p ON p.id = ph.post_id
            WHERE p.taken_at >= now() - INTERVAL '7 days'
            GROUP BY ph.hashtag
        ),
        prior_week AS (
            SELECT ph.hashtag,
                   COUNT(*) AS post_count,
                   AVG(p.score) AS avg_score
            FROM ig_post_hashtags ph
            JOIN ig_posts p ON p.id = ph.post_id
            WHERE p.taken_at >= now() - INTERVAL '14 days'
              AND p.taken_at <  now() - INTERVAL '7 days'
            GROUP BY ph.hashtag
        )
        SELECT COALESCE(l.hashtag, pr.hashtag) AS hashtag,
               COALESCE(l.post_count, 0)       AS last_week_count,
               COALESCE(pr.post_count, 0)      AS prior_week_count,
               COALESCE(l.avg_score, 0)        AS last_week_avg_score,
               COALESCE(pr.avg_score, 0)       AS prior_week_avg_score,
               COALESCE(l.post_count, 0) - COALESCE(pr.post_count, 0) AS post_delta
        FROM last_week l
        FULL OUTER JOIN prior_week pr ON l.hashtag = pr.hashtag
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX ig_hashtag_velocity_pk "
        "ON ig_hashtag_velocity (hashtag)"
    )


def downgrade() -> None:
    op.execute("DROP MATERIALIZED VIEW IF EXISTS ig_hashtag_velocity")
    op.execute("DROP MATERIALIZED VIEW IF EXISTS ig_author_posting_pattern")
    op.execute("DROP MATERIALIZED VIEW IF EXISTS ig_top_posts_by_author")
