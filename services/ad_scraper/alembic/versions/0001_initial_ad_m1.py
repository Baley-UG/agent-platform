"""Initial schema for ad_scraper (AD-M1).

Eight tables in the shared `public` schema behind the `ad_*` prefix:

    ad_scrape_jobs            operator-driven ingestion queue
    ad_credentials            YouCloud session (Fernet-encrypted secrets)
    ad_dimensions             generic facet lookup, PK (kind, code)
    ad_advertisers            App / AppBrand / Website / Playlet / Novel
    ad_materials              the ad creative
    ad_material_resources     the creative's resource[] array
    ad_material_dimensions    material x facet edges
    ad_material_advertisers   material x advertiser edges

Creation order matters: `ad_materials.discovered_via_job_id` references
`ad_scrape_jobs`, and both edge tables reference `ad_materials`.

Revision ID: 0001_initial_ad_m1
Revises:
Create Date: 2026-08-18

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial_ad_m1"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ---------- Ingestion queue ----------
    op.create_table(
        "ad_scrape_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("filters", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("page_from", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("page_to", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("order", sa.Text(), nullable=False, server_default="max_dt_desc"),
        # Nullable on purpose: NULL means "follow AD_MIRROR_MEDIA", while
        # true/false are the job's explicit override of it.
        sa.Column("mirror", sa.Boolean(), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(32), nullable=True),
        sa.Column("stats", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_ad_scrape_jobs_status", "ad_scrape_jobs", ["status"])
    op.create_index("ix_ad_scrape_jobs_status_created", "ad_scrape_jobs", ["status", "created_at"])

    # ---------- Credentials ----------
    # Token only — no username/password columns. Automatic login was
    # considered and dropped, so there is no password to store, leak, or get
    # the account locked out with.
    op.create_table(
        "ad_credentials",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("label", sa.String(64), nullable=False, server_default="default"),
        sa.Column("session_cookie_enc", sa.LargeBinary(), nullable=True),
        sa.Column("session_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="expired"),
        sa.Column("last_ok_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_ad_credentials_status", "ad_credentials", ["status"])

    # ---------- Generic facet lookup ----------
    op.create_table(
        "ad_dimensions",
        sa.Column("kind", sa.String(32), primary_key=True),
        sa.Column("code", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(255), nullable=True),
        sa.Column("icon", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("parent_code", sa.String(64), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    # ---------- Advertised entities ----------
    op.create_table(
        "ad_advertisers",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("kind", sa.String(32), nullable=True),
        sa.Column("type", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(512), nullable=True),
        sa.Column("icon", sa.Text(), nullable=True),
        sa.Column("types", postgresql.ARRAY(sa.Integer()), nullable=True),
        sa.Column("alias", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("gp_app_url", sa.Text(), nullable=True),
        sa.Column("ios_app_url", sa.Text(), nullable=True),
        sa.Column("minis_type", sa.Integer(), nullable=True),
        sa.Column("developer_id", sa.String(128), nullable=True),
        sa.Column("developer_name", sa.String(512), nullable=True),
        sa.Column("developer_area_cc", sa.String(8), nullable=True),
        sa.Column("raw", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_ad_advertisers_kind", "ad_advertisers", ["kind"])
    op.create_index("ix_ad_advertisers_name", "ad_advertisers", ["name"])
    op.create_index("ix_ad_advertisers_developer_id", "ad_advertisers", ["developer_id"])

    # ---------- The creative ----------
    op.create_table(
        "ad_materials",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("type", sa.Integer(), nullable=True),
        sa.Column("creative_type", sa.Integer(), nullable=True),
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("run_days", sa.Integer(), nullable=True),
        sa.Column("ad_count", sa.Integer(), nullable=True),
        sa.Column("similar_cnt", sa.Integer(), nullable=True),
        sa.Column("impression_inc_2y_raw", sa.String(32), nullable=True),
        sa.Column("impression_inc_2y", sa.BigInteger(), nullable=True),
        sa.Column("gender", sa.Integer(), nullable=True),
        sa.Column("violation", sa.Text(), nullable=True),
        sa.Column("slogan", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("txt_url", sa.Text(), nullable=True),
        sa.Column("asr", sa.Text(), nullable=True),
        sa.Column("media_format", sa.String(32), nullable=True),
        sa.Column("media_width", sa.Integer(), nullable=True),
        sa.Column("media_height", sa.Integer(), nullable=True),
        sa.Column("media_duration_sec", sa.Integer(), nullable=True),
        sa.Column("media_url", sa.Text(), nullable=True),
        sa.Column("poster_url", sa.Text(), nullable=True),
        sa.Column("media_url_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("media_s3_key", sa.String(512), nullable=True),
        sa.Column("poster_s3_key", sa.String(512), nullable=True),
        sa.Column("media_mirrored_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "discovered_via_job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ad_scrape_jobs.id"),
            nullable=True,
        ),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_ad_materials_type", "ad_materials", ["type"])
    op.create_index("ix_ad_materials_end_date", "ad_materials", ["end_date"])
    op.create_index("ix_ad_materials_run_days", "ad_materials", ["run_days"])
    op.create_index("ix_ad_materials_impression_inc_2y", "ad_materials", ["impression_inc_2y"])
    op.create_index("ix_ad_materials_last_seen_at", "ad_materials", ["last_seen_at"])

    # ---------- The creative's resource array ----------
    op.create_table(
        "ad_material_resources",
        sa.Column("material_id", sa.String(64), sa.ForeignKey("ad_materials.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("idx", sa.Integer(), primary_key=True),
        sa.Column("resource_id", sa.String(128), nullable=True),
        sa.Column("format", sa.String(32), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("duration_sec", sa.Integer(), nullable=True),
        sa.Column("path", sa.Text(), nullable=True),
        sa.Column("poster", sa.Text(), nullable=True),
        sa.Column("url_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("s3_key", sa.String(512), nullable=True),
    )

    # ---------- Edges ----------
    op.create_table(
        "ad_material_dimensions",
        sa.Column("material_id", sa.String(64), sa.ForeignKey("ad_materials.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("kind", sa.String(32), primary_key=True),
        sa.Column("code", sa.String(64), primary_key=True),
        sa.ForeignKeyConstraint(
            ["kind", "code"],
            ["ad_dimensions.kind", "ad_dimensions.code"],
            name="fk_ad_material_dimensions_dimension",
        ),
    )
    op.create_index(
        "ix_ad_material_dimensions_kind_code",
        "ad_material_dimensions",
        ["kind", "code", "material_id"],
    )

    op.create_table(
        "ad_material_advertisers",
        sa.Column("material_id", sa.String(64), sa.ForeignKey("ad_materials.id", ondelete="CASCADE"), primary_key=True),
        sa.Column(
            "advertiser_id",
            sa.String(128),
            sa.ForeignKey("ad_advertisers.id", ondelete="CASCADE"),
            primary_key=True,
        ),
    )
    op.create_index(
        "ix_ad_material_advertisers_advertiser",
        "ad_material_advertisers",
        ["advertiser_id", "material_id"],
    )


def downgrade() -> None:
    op.drop_table("ad_material_advertisers")
    op.drop_table("ad_material_dimensions")
    op.drop_table("ad_material_resources")
    op.drop_table("ad_materials")
    op.drop_table("ad_advertisers")
    op.drop_table("ad_dimensions")
    op.drop_table("ad_credentials")
    op.drop_table("ad_scrape_jobs")
