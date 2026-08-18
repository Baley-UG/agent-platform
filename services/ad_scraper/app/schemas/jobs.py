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

import re

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
    "max_dt",
    "min_dt_desc",
    "min_dt",
    "cnt_dt_desc",
    "cnt_dt",
    "cnt_ad_id_desc",
    "impression_inc_2y_desc",
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

# The upstream validates these strictly: ONE bad value fails the WHOLE request
# with "Parameter error, please clear the filter and refresh" — it is never
# ignored. All measured against the live endpoint.
#
# `area` is uppercase ISO-3166-1 alpha-2, one code per array element. Every
# other shape is rejected: lowercase `tr`, ISO-3 `TUR`, an empty string, and —
# the one that bit a real caller — two codes joined into one element
# (`"TR;SA"` or `"TR,SA"`). A panel that string-joins its country multi-select
# produces exactly that.
_AREA_RE = re.compile(r"^[A-Z]{2}$")

# Integer-code arrays. We check the shape, not a whitelist: the platform adds
# codes without notice (`media` alone spans 1-19, 21-23, 25-26, 28-34
# today), and rejecting a newly-valid code would be worse than forwarding it
# and letting the upstream answer.
_INT_CODE_KEYS = ("media", "platform", "format", "creativeType", "resourceElement", "category")


class JobCreate(BaseModel):
    """Create an ingestion job."""

    filters: Dict[str, Any] = Field(
        default_factory=dict,
        # Pydantic skips validators on defaults unless asked. Without this a
        # job posted with no `filters` at all keeps an empty dict and reaches
        # the upstream with no `purpose`, which is the one case the default
        # exists to cover.
        validate_default=True,
        description=(
            "materialList GraphQL variables, verbatim (media, area, platform, format, "
            "keyword, startDate/endDate, isAllDate, accurateSearch, ...). "
            "`purpose` defaults to AD_DEFAULT_PURPOSE when omitted; pass it to override. "
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
    def _default_purpose(cls, value: Dict[str, Any]) -> Dict[str, Any]:
        """Fill in `purpose` when the caller omits it.

        The upstream requires it (`$purpose: Int!`) and offers no "all", so
        every job needs a value. Rather than making each caller carry a
        constant, the service supplies `AD_DEFAULT_PURPOSE`. An explicit
        value always wins — the default is a convenience, not a lock.
        """
        value = dict(value or {})
        if value.get("purpose") is None:
            value["purpose"] = settings.AD_DEFAULT_PURPOSE
        return value

    @field_validator("filters")
    @classmethod
    def _reject_worker_owned_keys(cls, value: Dict[str, Any]) -> Dict[str, Any]:
        clashes = sorted(_WORKER_OWNED_KEYS & set(value or {}))
        if clashes:
            raise ValueError(f"filters must not contain {clashes} — use the page_from/page_to/order fields instead")
        return value

    @field_validator("filters")
    @classmethod
    def _validate_filter_values(cls, value: Dict[str, Any]) -> Dict[str, Any]:
        """Catch the malformed values the upstream rejects wholesale.

        Every check here mirrors a measured upstream behaviour. Forwarding
        these would turn a fixable mistake into a generic "Parameter error,
        please clear the filter and refresh" with no clue which field was
        wrong.
        """
        filters = value or {}

        area = filters.get("area")
        if area is not None:
            if not isinstance(area, list):
                raise ValueError('filters.area must be a list of country codes, e.g. ["TR", "US"]')
            for code in area:
                if not isinstance(code, str) or not _AREA_RE.match(code):
                    hint = ""
                    if isinstance(code, str) and any(sep in code for sep in ";,|/ "):
                        parts = [p for p in re.split(r"[;,|/\s]+", code) if p]
                        hint = f" Did you mean {parts}? Send one code per array element, not a joined string."
                    elif isinstance(code, str) and code.upper() != code:
                        hint = f" Codes are uppercase — try {code.upper()!r}."
                    raise ValueError(
                        f"filters.area contains {code!r}, which the upstream rejects. Each element must be an "
                        f"uppercase ISO-3166-1 alpha-2 country code (two letters).{hint}"
                    )

        for key in _INT_CODE_KEYS:
            codes = filters.get(key)
            if codes is None:
                continue
            if not isinstance(codes, list):
                raise ValueError(f"filters.{key} must be a list of integer codes, e.g. [13]")
            for code in codes:
                if isinstance(code, bool) or not isinstance(code, int) or code <= 0:
                    raise ValueError(
                        f"filters.{key} contains {code!r}; it must be a list of positive integer codes. "
                        f"An unknown code fails the whole upstream request rather than being ignored — "
                        f"read the valid values from GET /dimensions."
                    )

        return filters

    @model_validator(mode="after")
    def _check_date_window(self) -> "JobCreate":
        """Refuse the two date shapes that fail silently upstream.

        `isAllDate: 1` **overrides** `startDate`/`endDate` — sending both is
        accepted and quietly ignores the range (measured: identical totals
        with and without the dates). And an inverted range is accepted too,
        returning a nonsense subset rather than erroring.
        """
        f = self.filters or {}
        start, end = f.get("startDate"), f.get("endDate")
        if f.get("isAllDate") and (start or end):
            raise ValueError(
                "filters.isAllDate overrides startDate/endDate upstream — sending both silently ignores "
                "the date range. Drop isAllDate to use the range, or drop the range to scan all dates."
            )
        if isinstance(start, str) and isinstance(end, str) and start > end:
            raise ValueError(
                f"filters.startDate ({start}) is after endDate ({end}). The upstream accepts this and "
                "returns a nonsense subset instead of erroring, so it is refused here."
            )
        return self

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
