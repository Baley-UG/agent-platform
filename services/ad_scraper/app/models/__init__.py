"""SQLModel table definitions for ad_scraper.

Importing this package registers every table on `SQLModel.metadata`,
which is what `alembic/env.py` relies on for autogenerate.

All tables live in the shared `public` schema behind the `ad_*` prefix —
the same decision ig_scraper made with `ig_*`. Cross-service joins
(content_pipeline reading `ad_materials`) stay trivial.
"""

from app.models.advertiser import Advertiser, MaterialAdvertiser
from app.models.base import new_uuid, utcnow
from app.models.credential import (
    ACTIVE,
    DISABLED,
    EXPIRED,
    LOGIN_FAILED,
    VALID_CREDENTIAL_STATUSES,
    Credential,
)
from app.models.dimension import DIMENSION_KINDS, Dimension, MaterialDimension
from app.models.job import (
    CANCELLED,
    FAILED,
    QUEUED,
    RUNNING,
    SUCCEEDED,
    TERMINAL_STATUSES,
    VALID_STATUSES,
    ScrapeJob,
)
from app.models.material import Material, MaterialResource

__all__ = [
    "Advertiser",
    "MaterialAdvertiser",
    "Credential",
    "Dimension",
    "MaterialDimension",
    "DIMENSION_KINDS",
    "ScrapeJob",
    "Material",
    "MaterialResource",
    "utcnow",
    "new_uuid",
    "QUEUED",
    "RUNNING",
    "SUCCEEDED",
    "FAILED",
    "CANCELLED",
    "TERMINAL_STATUSES",
    "VALID_STATUSES",
    "ACTIVE",
    "EXPIRED",
    "LOGIN_FAILED",
    "DISABLED",
    "VALID_CREDENTIAL_STATUSES",
]
