"""Initial Phase-1 schema for ig_scraper.

Creates every table the Phase-1 milestones need. Each later milestone
will add columns or tables via its own migration; this one is the
full snapshot of the state at the end of M1.

Revision ID: 0001_initial_phase1
Revises:
Create Date: 2026-05-06

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial_phase1"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ---------- Infra: proxies ----------
    op.create_table(
        "ig_proxies",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("protocol", sa.Text(), nullable=False),
        sa.Column("host", sa.Text(), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("username", sa.Text(), nullable=True),
        sa.Column("password_enc", sa.LargeBinary(), nullable=True),
        sa.Column("label", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("last_ok_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cooldown_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    # ---------- Infra: accounts ----------
    op.create_table(
        "ig_accounts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("password_enc", sa.LargeBinary(), nullable=False),
        sa.Column("session_blob", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="disabled"),
        sa.Column("role", sa.Text(), nullable=False, server_default="scraper"),
        sa.Column("proxy_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("ig_proxies.id"), nullable=True),
        sa.Column("timezone", sa.Text(), nullable=False, server_default="UTC"),
        sa.Column("active_hours_start", sa.SmallInteger(), nullable=False, server_default="8"),
        sa.Column("active_hours_end", sa.SmallInteger(), nullable=False, server_default="23"),
        sa.Column("weekday_pattern", sa.SmallInteger(), nullable=False, server_default="127"),
        sa.Column("quota_tier", sa.Text(), nullable=False, server_default="fresh"),
        sa.Column("cooldown_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("username", name="uq_ig_accounts_username"),
    )
    op.create_index("ix_ig_accounts_username", "ig_accounts", ["username"], unique=True)

    # ---------- Content: users (scraping targets) ----------
    op.create_table(
        "ig_users",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("full_name", sa.Text(), nullable=True),
        sa.Column("biography", sa.Text(), nullable=True),
        sa.Column("follower_count", sa.Integer(), nullable=True),
        sa.Column("following_count", sa.Integer(), nullable=True),
        sa.Column("media_count", sa.Integer(), nullable=True),
        sa.Column("is_business", sa.Boolean(), nullable=True),
        sa.Column("is_verified", sa.Boolean(), nullable=True),
        sa.Column("is_private", sa.Boolean(), nullable=True),
        sa.Column("profile_pic_url", sa.Text(), nullable=True),
        sa.Column("biography_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("username", name="uq_ig_users_username"),
    )
    op.create_index("ix_ig_users_username", "ig_users", ["username"], unique=True)

    # ---------- Content: hashtags ----------
    op.create_table(
        "ig_hashtags",
        sa.Column("name", sa.Text(), primary_key=True),
        sa.Column("media_count", sa.BigInteger(), nullable=True),
        sa.Column("last_scanned_at", sa.DateTime(timezone=True), nullable=True),
    )

    # ---------- Content: audio ----------
    op.create_table(
        "ig_audio_tracks",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("artist", sa.Text(), nullable=True),
        sa.Column(
            "original_audio_user_id",
            sa.BigInteger(),
            sa.ForeignKey("ig_users.id"),
            nullable=True,
        ),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("use_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("raw", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    # ---------- Job queue (forward-ref'd by posts/stories/targets) ----------
    op.create_table(
        "ig_scrape_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("job_type", sa.Text(), nullable=False),
        sa.Column("target", sa.Text(), nullable=False),
        sa.Column("scan_target_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("params", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("min_likes", sa.Integer(), nullable=True),
        sa.Column("min_impressions", sa.Integer(), nullable=True),
        sa.Column(
            "account_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("ig_accounts.id"), nullable=True
        ),
        sa.Column("proxy_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("ig_proxies.id"), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("stats", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_ig_scrape_jobs_status_scheduled", "ig_scrape_jobs", ["status", "scheduled_for"]
    )

    # ---------- Tracked targets (FK back-ref to scrape_jobs.last_run_job_id) ----------
    op.create_table(
        "ig_scan_targets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("interval_hours", sa.Integer(), nullable=False, server_default="24"),
        sa.Column("fetch_feed", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("fetch_stories", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("fetch_highlights", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("fetch_comments", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("comment_limit", sa.Integer(), nullable=False, server_default="50"),
        sa.Column("min_likes", sa.Integer(), nullable=True),
        sa.Column("min_impressions", sa.Integer(), nullable=True),
        sa.Column("hashtag_section", sa.Text(), nullable=False, server_default="top"),
        sa.Column("first_backfill_done", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_seen_post_id", sa.BigInteger(), nullable=True),
        sa.Column("last_seen_taken_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column(
            "last_run_job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ig_scrape_jobs.id"),
            nullable=True,
        ),
        sa.Column("auto_discovered", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("source_target_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("kind", "value", name="uq_scan_targets_kind_value"),
    )
    op.create_index(
        "ix_ig_scan_targets_status_next_run", "ig_scan_targets", ["status", "next_run_at"]
    )
    # Self-referential FK (auto-discovered targets point back at the source hashtag).
    op.create_foreign_key(
        "fk_scan_targets_source",
        "ig_scan_targets",
        "ig_scan_targets",
        ["source_target_id"],
        ["id"],
    )
    # Late FK from scrape_jobs.scan_target_id → scan_targets.id (cycle-broken).
    op.create_foreign_key(
        "fk_scrape_jobs_scan_target",
        "ig_scrape_jobs",
        "ig_scan_targets",
        ["scan_target_id"],
        ["id"],
    )

    # ---------- Content: posts ----------
    op.create_table(
        "ig_posts",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("media_type", sa.SmallInteger(), nullable=False),
        sa.Column("product_type", sa.Text(), nullable=True),
        sa.Column("author_id", sa.BigInteger(), sa.ForeignKey("ig_users.id"), nullable=False),
        sa.Column("caption", sa.Text(), nullable=True),
        sa.Column("taken_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("like_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("comment_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("play_count", sa.BigInteger(), nullable=True),
        sa.Column("view_count", sa.BigInteger(), nullable=True),
        sa.Column("save_count", sa.BigInteger(), nullable=True),
        sa.Column("video_duration", sa.Float(), nullable=True),
        sa.Column("thumbnail_url", sa.Text(), nullable=True),
        sa.Column("media_urls", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        # S3 mirror columns — IG CDN URLs expire in 1-7 days, so we
        # copy "important" posts (tracked authors, or manually pinned)
        # into our bucket at scrape time. Null = not mirrored yet.
        # See app/services/mirror.py for the policy.
        sa.Column("media_s3_key", sa.Text(), nullable=True),
        sa.Column("poster_s3_key", sa.Text(), nullable=True),
        sa.Column("media_mirrored_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("location", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("music_info", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("audio_track_id", sa.Text(), sa.ForeignKey("ig_audio_tracks.id"), nullable=True),
        sa.Column("hashtags", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("mentions", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("language", sa.Text(), nullable=True),
        sa.Column("emoji_count", sa.Integer(), nullable=True),
        sa.Column("hashtag_count", sa.Integer(), nullable=True),
        sa.Column("mention_count", sa.Integer(), nullable=True),
        sa.Column("caption_length", sa.Integer(), nullable=True),
        sa.Column("has_question", sa.Boolean(), nullable=True),
        sa.Column("has_cta", sa.Boolean(), nullable=True),
        sa.Column("caption_simhash", sa.BigInteger(), nullable=True),
        sa.Column("score", sa.Numeric(5, 2), nullable=True),
        sa.Column("score_components", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("score_computed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "discovered_via_job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ig_scrape_jobs.id"),
            nullable=True,
        ),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("code", name="uq_ig_posts_code"),
    )
    op.create_index("ix_ig_posts_code", "ig_posts", ["code"], unique=True)
    op.create_index("ix_ig_posts_author_taken", "ig_posts", ["author_id", sa.text("taken_at DESC")])
    op.create_index("ix_ig_posts_like_count", "ig_posts", ["like_count"])
    op.create_index("ix_ig_posts_play_count", "ig_posts", ["play_count"])
    op.create_index("ix_ig_posts_score", "ig_posts", ["score"])
    op.create_index("ix_ig_posts_simhash", "ig_posts", ["caption_simhash"])

    # ---------- Content: post_hashtags ----------
    op.create_table(
        "ig_post_hashtags",
        sa.Column("post_id", sa.BigInteger(), sa.ForeignKey("ig_posts.id"), primary_key=True),
        sa.Column("hashtag", sa.Text(), sa.ForeignKey("ig_hashtags.name"), primary_key=True),
    )
    op.create_index("ix_ig_post_hashtags_hashtag", "ig_post_hashtags", ["hashtag"])

    # ---------- Content: post metric snapshots ----------
    op.create_table(
        "ig_post_metric_snapshots",
        sa.Column("post_id", sa.BigInteger(), sa.ForeignKey("ig_posts.id"), primary_key=True),
        sa.Column("scanned_at", sa.DateTime(timezone=True), primary_key=True, server_default=sa.func.now()),
        sa.Column("like_count", sa.Integer(), nullable=False),
        sa.Column("comment_count", sa.Integer(), nullable=False),
        sa.Column("play_count", sa.BigInteger(), nullable=True),
        sa.Column("view_count", sa.BigInteger(), nullable=True),
        sa.Column("save_count", sa.BigInteger(), nullable=True),
        sa.Column("score", sa.Numeric(5, 2), nullable=True),
    )
    op.create_index(
        "ix_ig_post_metric_snapshots_post_scanned",
        "ig_post_metric_snapshots",
        ["post_id", sa.text("scanned_at DESC")],
    )

    # ---------- Content: comments ----------
    op.create_table(
        "ig_comments",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("post_id", sa.BigInteger(), sa.ForeignKey("ig_posts.id"), nullable=False),
        sa.Column("author_id", sa.BigInteger(), sa.ForeignKey("ig_users.id"), nullable=False),
        sa.Column("parent_comment_id", sa.BigInteger(), nullable=True),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("like_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at_ig", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("text_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_ig_comments_post", "ig_comments", ["post_id"])
    op.create_index("ix_ig_comments_author", "ig_comments", ["author_id"])

    # ---------- Content: stories ----------
    op.create_table(
        "ig_stories",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("author_id", sa.BigInteger(), sa.ForeignKey("ig_users.id"), nullable=False),
        sa.Column("media_type", sa.SmallInteger(), nullable=False),
        sa.Column("taken_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("video_duration", sa.Float(), nullable=True),
        sa.Column("media_url", sa.Text(), nullable=True),
        sa.Column("thumbnail_url", sa.Text(), nullable=True),
        sa.Column("caption", sa.Text(), nullable=True),
        sa.Column("mentions", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("hashtags", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("link_sticker_url", sa.Text(), nullable=True),
        sa.Column("seen_count", sa.Integer(), nullable=True),
        sa.Column("raw", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "discovered_via_job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ig_scrape_jobs.id"),
            nullable=True,
        ),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_ig_stories_author_taken", "ig_stories", ["author_id", sa.text("taken_at DESC")])

    # ---------- Content: highlights ----------
    op.create_table(
        "ig_highlights",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("owner_id", sa.BigInteger(), sa.ForeignKey("ig_users.id"), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("cover_url", sa.Text(), nullable=True),
        sa.Column("media_count", sa.Integer(), nullable=True),
        sa.Column("raw", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("last_scanned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_ig_highlights_owner", "ig_highlights", ["owner_id"])

    op.create_table(
        "ig_highlight_items",
        sa.Column(
            "highlight_id", sa.BigInteger(), sa.ForeignKey("ig_highlights.id"), primary_key=True
        ),
        sa.Column("story_id", sa.BigInteger(), sa.ForeignKey("ig_stories.id"), primary_key=True),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
    )

    # ---------- Webhooks ----------
    op.create_table(
        "ig_webhooks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("secret", sa.Text(), nullable=True),
        sa.Column("filters", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("last_delivery_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_delivery_status", sa.Integer(), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_ig_webhooks_event_type", "ig_webhooks", ["event_type"])

    # ---------- Usage tracking ----------
    op.create_table(
        "ig_usage_daily",
        sa.Column("date", sa.Date(), primary_key=True),
        sa.Column(
            "account_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("ig_accounts.id"), primary_key=True
        ),
        sa.Column("calls_made", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("posts_saved", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("comments_saved", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("stories_saved", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("proxy_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("challenge_count", sa.Integer(), nullable=False, server_default="0"),
    )

    # ---------- Worker / scheduler heartbeat ----------
    op.create_table(
        "ig_worker_heartbeat",
        sa.Column("process", sa.Text(), primary_key=True),
        sa.Column("instance_id", sa.Text(), primary_key=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("pid", sa.Integer(), nullable=True),
        sa.Column("version", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    # Drop in reverse dependency order.
    op.drop_table("ig_worker_heartbeat")
    op.drop_table("ig_usage_daily")
    op.drop_index("ix_ig_webhooks_event_type", table_name="ig_webhooks")
    op.drop_table("ig_webhooks")
    op.drop_table("ig_highlight_items")
    op.drop_index("ix_ig_highlights_owner", table_name="ig_highlights")
    op.drop_table("ig_highlights")
    op.drop_index("ix_ig_stories_author_taken", table_name="ig_stories")
    op.drop_table("ig_stories")
    op.drop_index("ix_ig_comments_author", table_name="ig_comments")
    op.drop_index("ix_ig_comments_post", table_name="ig_comments")
    op.drop_table("ig_comments")
    op.drop_index(
        "ix_ig_post_metric_snapshots_post_scanned", table_name="ig_post_metric_snapshots"
    )
    op.drop_table("ig_post_metric_snapshots")
    op.drop_index("ix_ig_post_hashtags_hashtag", table_name="ig_post_hashtags")
    op.drop_table("ig_post_hashtags")
    op.drop_index("ix_ig_posts_simhash", table_name="ig_posts")
    op.drop_index("ix_ig_posts_score", table_name="ig_posts")
    op.drop_index("ix_ig_posts_play_count", table_name="ig_posts")
    op.drop_index("ix_ig_posts_like_count", table_name="ig_posts")
    op.drop_index("ix_ig_posts_author_taken", table_name="ig_posts")
    op.drop_index("ix_ig_posts_code", table_name="ig_posts")
    op.drop_table("ig_posts")
    op.drop_constraint("fk_scrape_jobs_scan_target", "ig_scrape_jobs", type_="foreignkey")
    op.drop_constraint("fk_scan_targets_source", "ig_scan_targets", type_="foreignkey")
    op.drop_index("ix_ig_scan_targets_status_next_run", table_name="ig_scan_targets")
    op.drop_table("ig_scan_targets")
    op.drop_index("ix_ig_scrape_jobs_status_scheduled", table_name="ig_scrape_jobs")
    op.drop_table("ig_scrape_jobs")
    op.drop_table("ig_audio_tracks")
    op.drop_table("ig_hashtags")
    op.drop_index("ix_ig_users_username", table_name="ig_users")
    op.drop_table("ig_users")
    op.drop_index("ix_ig_accounts_username", table_name="ig_accounts")
    op.drop_table("ig_accounts")
    op.drop_table("ig_proxies")
