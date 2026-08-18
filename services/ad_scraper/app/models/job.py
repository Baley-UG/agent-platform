"""ad_scrape_jobs — single source of truth for queued/running/finished work.

Ingestion is operator-driven: there is no scheduler and no saved-search
registry. Each job carries the `materialList` GraphQL variables verbatim
in `filters` plus the page window to walk.

The API caps `page` at 200 with a server-fixed `limit` of 50, so one job
can never see more than 10 000 rows no matter what it asks for. When the
API reports a `total` larger than the window can return, the worker sets
`stats.truncated = true` — the ceiling is a visible signal, never a
silent cut. Narrow the filter (date window, area, media, platform,
keyword) and run several jobs.
"""

import uuid
from datetime import datetime
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Column, Field, SQLModel

from app.models.base import new_uuid, utcnow

QUEUED = "queued"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCELLED = "cancelled"

TERMINAL_STATUSES: frozenset[str] = frozenset({SUCCEEDED, FAILED, CANCELLED})
VALID_STATUSES: frozenset[str] = frozenset({QUEUED, RUNNING, *TERMINAL_STATUSES})


class ScrapeJob(SQLModel, table=True):
    """An ingestion job claimed by the worker via SELECT ... FOR UPDATE SKIP LOCKED."""

    __tablename__ = "ad_scrape_jobs"
    __table_args__ = (sa.Index("ix_ad_scrape_jobs_status_created", "status", "created_at"),)

    id: uuid.UUID = Field(default_factory=new_uuid, primary_key=True)

    status: str = Field(default=QUEUED, index=True)
    # The GraphQL `variables` object minus `page`/`order`, which the worker
    # supplies per request. Stored as given so a job is reproducible.
    filters: Optional[dict] = Field(default=None, sa_column=Column("filters", JSONB, nullable=True))
    page_from: int = Field(default=1)
    page_to: int = Field(default=5)
    order: str = Field(default="max_dt_desc")
    # Tri-state mirror intent. NULL = follow `AD_MIRROR_MEDIA`; true/false are
    # the operator's explicit override. Under the `always` policy an explicit
    # false now wins — it used to be silently ignored, which is the same
    # failure shape as a filter that returns nothing without erroring.
    mirror: Optional[bool] = Field(default=None)

    attempt: int = Field(default=0)
    max_attempts: int = Field(default=3)
    error: Optional[str] = Field(default=None)
    # YouCloud's `errors[0].extensions.c` value, e.g. `05:400001`. Kept
    # separate from the message so failures can be grouped without
    # string-matching a localised sentence.
    error_code: Optional[str] = Field(default=None, max_length=32)

    stats: Optional[dict] = Field(default=None, sa_column=Column("stats", JSONB, nullable=True))

    created_at: datetime = Field(default_factory=utcnow)
    started_at: Optional[datetime] = Field(default=None)
    finished_at: Optional[datetime] = Field(default=None)
