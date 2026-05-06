"""ig_usage_daily — per-account/day cost & call tracking."""

import datetime as _dt
import uuid

from sqlmodel import Field, SQLModel


class UsageDaily(SQLModel, table=True):
    """Aggregated daily usage stats for capacity / cost analysis.

    Worker increments `calls_made` and friends inline; a nightly job
    consolidates per-account/day totals.
    """

    __tablename__ = "ig_usage_daily"

    date: _dt.date = Field(primary_key=True)
    account_id: uuid.UUID = Field(primary_key=True, foreign_key="ig_accounts.id")
    calls_made: int = Field(default=0)
    posts_saved: int = Field(default=0)
    comments_saved: int = Field(default=0)
    stories_saved: int = Field(default=0)
    proxy_bytes: int = Field(default=0)
    challenge_count: int = Field(default=0)
