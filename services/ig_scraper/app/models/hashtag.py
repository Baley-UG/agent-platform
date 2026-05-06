"""ig_hashtags — hashtag dimension table."""

from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel


class Hashtag(SQLModel, table=True):
    """A normalised hashtag.

    Name is stored lower-cased, without leading `#`. `media_count` is a
    snapshot of the IG-reported size at last scan.
    """

    __tablename__ = "ig_hashtags"

    name: str = Field(primary_key=True)
    media_count: Optional[int] = Field(default=None)
    last_scanned_at: Optional[datetime] = Field(default=None)
