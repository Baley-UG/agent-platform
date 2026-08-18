"""ad_materials, ad_material_resources — the creative itself.

A "material" is one ad creative as AppGrowing sees it: a 32-hex id, a
media file (mp4 or jpeg), and the aggregate stats of every ad that used
it. The same creative can be run by dozens of advertisers across many
countries and channels; those relations live in `ad_advertisers` and
`ad_dimensions`.

Two field names deserve a warning, because the API's names are
misleading:

* the API's `material.duration` is the number of DAYS the creative has
  been running (`end_date - start_date`), NOT the video length. We store
  it as `run_days`.
* the video length in seconds comes from `creative.resource[].duration`
  and is stored as `media_duration_sec`.
"""

import uuid
from datetime import date, datetime
from typing import Optional

from sqlalchemy import Column
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from app.models.base import utcnow


class Material(SQLModel, table=True):
    """One ad creative.

    The primary resource (first entry of `creative.resource[]`) is
    denormalised onto this row so the common "list creatives" query needs
    no join. The full array still lands in `ad_material_resources` for
    the rare multi-resource creative.
    """

    __tablename__ = "ad_materials"

    id: str = Field(primary_key=True, max_length=64, description="AppGrowing material id (32-hex).")

    # 102 = image / banner, 202 = video. Kept as the raw int rather than an
    # enum because the platform adds codes without notice.
    type: Optional[int] = Field(default=None, index=True)
    creative_type: Optional[int] = Field(default=None)

    # First / last day the creative was observed live, straight from the API.
    start_date: Optional[date] = Field(default=None)
    end_date: Optional[date] = Field(default=None, index=True)
    run_days: Optional[int] = Field(default=None, index=True, description="API `duration` — days on air, not seconds.")

    ad_count: Optional[int] = Field(default=None, description="API `cnt_ad_id` — distinct ads using this creative.")
    similar_cnt: Optional[int] = Field(default=None)

    # The API returns impressions pre-formatted ("1.1M", "476.3K"). We keep
    # the raw string for display fidelity and a parsed integer for sorting.
    impression_inc_2y_raw: Optional[str] = Field(default=None, max_length=32)
    impression_inc_2y: Optional[int] = Field(default=None, index=True)

    # Targeting / moderation signals. `violation` arrives as a plain label
    # string ("Human Exploitation") on the ~2% of creatives that carry one,
    # so it is stored as text rather than JSONB — `WHERE violation IS NOT
    # NULL` is then the whole query. Should the platform ever send a
    # structured value, the persistence layer serialises it to JSON text and
    # the untouched original is still in `raw`.
    gender: Optional[int] = Field(default=None, description="Targeting gender code (1/2/3).")
    violation: Optional[str] = Field(default=None, description="Moderation label, e.g. 'Human Exploitation'.")

    # Copy. `asr` is the platform's auto-transcript — populated on roughly
    # a fifth of video creatives and the single most useful field for the
    # content_pipeline analyzer.
    slogan: Optional[str] = Field(default=None)
    description: Optional[str] = Field(default=None)
    txt_url: Optional[str] = Field(default=None)
    asr: Optional[str] = Field(default=None)

    # Denormalised primary resource.
    media_format: Optional[str] = Field(default=None, max_length=32)
    media_width: Optional[int] = Field(default=None)
    media_height: Optional[int] = Field(default=None)
    media_duration_sec: Optional[int] = Field(default=None, description="Video length in seconds.")
    media_url: Optional[str] = Field(default=None)
    poster_url: Optional[str] = Field(default=None)
    # Parsed from the `auth_key=<epoch>-...` query param. Once past, the
    # CDN returns 403 and only the S3 mirror can serve the file.
    media_url_expires_at: Optional[datetime] = Field(default=None)

    # S3 mirror — see `app/services/mirror.py`.
    media_s3_key: Optional[str] = Field(default=None, max_length=512)
    poster_s3_key: Optional[str] = Field(default=None, max_length=512)
    media_mirrored_at: Optional[datetime] = Field(default=None)

    # Full material payload. Same rationale as `ig_posts.raw`: when the
    # extractor improves we re-derive columns without re-scraping.
    raw: Optional[dict] = Field(default=None, sa_column=Column("raw", JSONB, nullable=True))

    discovered_via_job_id: Optional[uuid.UUID] = Field(default=None, foreign_key="ad_scrape_jobs.id")
    first_seen_at: datetime = Field(default_factory=utcnow)
    last_seen_at: datetime = Field(default_factory=utcnow, index=True)


class MaterialResource(SQLModel, table=True):
    """One entry of the creative's `resource[]` array.

    Almost always a single row per material; carousels and multi-size
    creatives are the reason this is a table rather than more columns.
    """

    __tablename__ = "ad_material_resources"

    material_id: str = Field(foreign_key="ad_materials.id", primary_key=True, max_length=64)
    idx: int = Field(primary_key=True, description="Position within the API's resource array.")

    resource_id: Optional[str] = Field(default=None, max_length=128, description="API `resource.id` (opaque).")
    format: Optional[str] = Field(default=None, max_length=32)
    width: Optional[int] = Field(default=None)
    height: Optional[int] = Field(default=None)
    duration_sec: Optional[int] = Field(default=None)
    path: Optional[str] = Field(default=None)
    poster: Optional[str] = Field(default=None)
    url_expires_at: Optional[datetime] = Field(default=None)
    s3_key: Optional[str] = Field(default=None, max_length=512)
