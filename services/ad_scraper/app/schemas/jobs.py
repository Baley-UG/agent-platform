"""Request/response shapes for ingestion jobs.

`filters` is the `materialList` GraphQL `variables` object, passed through
verbatim. That is deliberate: an operator can build a query in the
AppGrowing web UI, copy the variables out of the network tab, and paste
them here without translation. We validate only the parts that carry a
hard server constraint — `page`/`order` are supplied by the worker per
request, so they are rejected inside `filters` to avoid two sources of
truth.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.config import settings

# Sort values seen on `MaterialListSort`. Not exhaustive and NOT enforced:
# the platform adds sorts without notice, and rejecting a valid-but-unlisted
# one is worse than passing a typo through (which the API answers with a
# clear parameter error). Confirmed against the live endpoint:
#   max_dt_desc              newest first — the default
#   impression_inc_2y_desc   highest 2-year impressions first
KNOWN_ORDERS: tuple[str, ...] = (
    "max_dt_desc",
    "impression_inc_2y_desc",
    "min_dt_desc",
    "duration_desc",
    "similar_cnt_desc",
)

# Keys the worker owns. Accepting them in `filters` would let a job carry
# a page number that silently contradicts `page_from`/`page_to`.
_WORKER_OWNED_KEYS = frozenset({"page", "order"})

# The web UI's "advanced" filter panel is the `searchDsl` variable — its URL
# carries an `advanced` param holding objects of key / value / type, with key
# "appid" for a store app id. App-id filtering goes through THERE, not
# through `campaign`. This matters because `campaign` handed a numeric store
# id is accepted and returns **zero rows** — no error, no warning, just an
# empty result that reads like "this app has no ads". `app_id` on JobCreate
# builds the right DSL entry so nobody has to rediscover that.
_APPID_DSL_KEY = "appid"


class JobCreate(BaseModel):
    """Create an ingestion job."""

    filters: Dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "materialList GraphQL variables, verbatim (purpose, media, area, platform, "
            "format, keyword, startDate/endDate, isAllDate, accurateSearch, ...). "
            "Omit `page` and `order` — those come from page_from/page_to/order."
        ),
    )
    app_id: Optional[str] = Field(
        default=None,
        max_length=64,
        description=(
            "Store app id (e.g. the 1661308505 in an App Store URL). Compiled into "
            "the `searchDsl` entry the platform actually filters on. Do NOT put an "
            "app id in `filters.campaign` — that returns zero rows without erroring."
        ),
    )
    page_from: int = Field(default=1, ge=1)
    page_to: Optional[int] = Field(default=None, ge=1)
    order: str = Field(default="max_dt_desc")
    mirror: Optional[bool] = Field(
        default=None,
        description=(
            "Mirror media to S3. Omit to follow AD_MIRROR_MEDIA (default `always`). "
            "Set false to opt this job out even under `always`; set true to opt in "
            "under the `job` policy. `never` ignores it."
        ),
    )
    max_attempts: int = Field(default=3, ge=1, le=10)

    @field_validator("filters")
    @classmethod
    def _reject_worker_owned_keys(cls, value: Dict[str, Any]) -> Dict[str, Any]:
        clashes = sorted(_WORKER_OWNED_KEYS & set(value or {}))
        if clashes:
            raise ValueError(f"filters must not contain {clashes} — use the page_from/page_to/order fields instead")
        return value

    @model_validator(mode="after")
    def _compile_app_id(self) -> "JobCreate":
        """Fold `app_id` into `filters.searchDsl`.

        Appends rather than replaces, so a hand-pasted `searchDsl` carrying
        other advanced clauses survives. A duplicate `appid` clause is
        refused instead of silently ANDed with itself.
        """
        if not self.app_id:
            return self
        dsl = self.filters.get("searchDsl")
        if dsl is None:
            dsl = []
        if not isinstance(dsl, list):
            raise ValueError("filters.searchDsl must be a list when app_id is set")
        if any(isinstance(e, dict) and e.get("key") == _APPID_DSL_KEY for e in dsl):
            raise ValueError(
                "filters.searchDsl already carries an 'appid' clause — set app_id OR the clause, not both"
            )
        self.filters = {
            **self.filters,
            "searchDsl": [*dsl, {"key": _APPID_DSL_KEY, "value": self.app_id, "type": "equal"}],
        }
        return self

    @model_validator(mode="after")
    def _warn_on_numeric_campaign(self) -> "JobCreate":
        """Reject the mistake that returns zero rows instead of an error.

        `campaign` expects the platform's opaque entity id (base64-ish, e.g.
        `M3OgFwcr4yLVdauQFomkHA==`). Handed a numeric store id it matches
        nothing and the API answers 200 with `total: 0` — verified. Failing
        the request is the only way that doesn't look like real data.
        """
        campaign = self.filters.get("campaign")
        if isinstance(campaign, str) and campaign.strip().isdigit():
            raise ValueError(
                f"filters.campaign='{campaign}' looks like a numeric store app id. "
                "`campaign` takes the platform's opaque entity id and returns zero rows "
                "for a numeric one, without erroring. Use the `app_id` field instead."
            )
        return self

    @model_validator(mode="after")
    def _check_page_window(self) -> "JobCreate":
        last = self.page_to if self.page_to is not None else settings.AD_DEFAULT_PAGE_TO
        if last < self.page_from:
            raise ValueError(f"page_to ({last}) must be >= page_from ({self.page_from})")
        if last > settings.AD_MAX_PAGE:
            # The server rejects page > 200 outright. Refusing here turns a
            # mid-job failure into an immediate, explainable 422.
            raise ValueError(
                f"page_to ({last}) exceeds the API ceiling of {settings.AD_MAX_PAGE}. "
                f"One filter set can never yield more than {settings.max_rows_per_filter_set} rows — "
                "narrow the filter (date window, area, media, platform, keyword) and run several jobs."
            )
        self.page_to = last
        return self


class JobRead(BaseModel):
    """A job row as the API returns it."""

    id: uuid.UUID
    status: str
    filters: Optional[Dict[str, Any]] = None
    page_from: int
    page_to: int
    order: str
    mirror: Optional[bool]
    attempt: int
    max_attempts: int
    error: Optional[str] = None
    error_code: Optional[str] = None
    stats: Optional[Dict[str, Any]] = None
    created_at: datetime
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None

    model_config = {"from_attributes": True}
