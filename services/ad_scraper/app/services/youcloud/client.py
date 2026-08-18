"""Async HTTP client for the YouCloud/AppGrowing GraphQL endpoint.

Thin wrapper around httpx with:
  * the exact header set the endpoint validates (see below)
  * session-cookie injection from `ad_credentials`
  * body-based error classification (`app.services.youcloud.errors`)
  * `AuthExpired` surfaced immediately — only an operator can fix it
  * a page-ceiling guard that fails fast instead of round-tripping
  * a process-wide rate gate in front of every request, so pacing is a
    property of the process rather than of one job — see
    `app.services.youcloud.throttle`

Header notes — these are requirements, not cargo cult:
  * `accept-language` is mandatory. Omit it and the endpoint answers
    HTTP 406 with a plain-text body, before any GraphQL parsing.
  * `origin` / `referer` are validated as a pair; they track the public
    web app, not this service.
  * `x-operation-name` mirrors `operationName`. The endpoint tolerates its
    absence today, but the web app always sends it and matching the
    client fingerprint costs nothing.

`trust_env=False` for the same reason ig_scraper's HikerAPI client does
it: ambient HTTP_PROXY / HTTPS_PROXY vars set for other scrapers must not
silently reroute these calls.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any, AsyncIterator, Dict, Optional

import httpx

from app.core.config import settings
from app.core.logging import logger
from app.core.metrics import (
    ad_api_errors_total,
    ad_pages_fetched_total,
    ad_rate_limited_total,
    ad_throttle_interval_seconds,
    ad_throttle_wait_seconds_total,
)
from app.services.youcloud.errors import (
    AuthExpired,
    BadFilter,
    RateLimited,
    TransportError,
    TransientError,
    YouCloudError,
    classify,
    metric_label,
)
from app.services.youcloud.throttle import Throttle, shared_throttle
from app.services.youcloud.queries import MATERIAL_LIST_OPERATION, MATERIAL_LIST_QUERY


class YouCloudClient:
    """Reusable async client. Construct once per job; close at the end.

    `session_provider` is an awaitable returning the current `sessionId`
    token. It is injected rather than imported so the client stays testable
    without a database — see `tests/test_youcloud_client.py`.

    There is no refresh hook. The token is the only auth mechanism and only
    an operator can produce a new one, so `AuthExpired` is surfaced
    immediately: the job records "token rejected, paste a fresh one" instead
    of retrying its way to the same conclusion.

    `throttle` defaults to the process-wide gate. Pass one explicitly only
    in tests — a second live gate would defeat the point of having one.
    """

    def __init__(self, *, session_provider, throttle: Optional[Throttle] = None) -> None:
        """Build a client over the injected session provider."""
        self._session_provider = session_provider
        self._throttle = throttle if throttle is not None else shared_throttle()
        # Per-client, so a job can report how long IT spent waiting on the
        # shared gate. The gate's own total mixes concurrent jobs together.
        self._waited_seconds = 0.0
        self._http = httpx.AsyncClient(
            timeout=settings.AD_API_TIMEOUT_SECONDS,
            headers={
                "accept": "application/json, text/plain, */*",
                # Mandatory — a missing value yields HTTP 406, not a GraphQL error.
                "accept-language": settings.AD_API_LANGUAGE,
                "content-type": "application/json",
                "origin": settings.AD_API_ORIGIN,
                "referer": settings.AD_API_REFERER,
                "user-agent": settings.AD_API_USER_AGENT,
            },
            trust_env=False,
            follow_redirects=False,
        )

    @property
    def waited_seconds(self) -> float:
        """Seconds this client spent waiting at the shared rate gate.

        Surfaced onto the job row: "slow" and "paced" look identical from the
        outside otherwise, and the difference decides whether to widen the
        interval or go looking for a real problem.
        """
        return self._waited_seconds

    async def __aenter__(self) -> "YouCloudClient":
        """Enter the async context; the client is ready on construction."""
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        """Close the connection pool on context exit."""
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying connection pool."""
        await self._http.aclose()

    # ------------------------------------------------------------------
    # Core request
    # ------------------------------------------------------------------

    async def execute(
        self,
        query: str,
        variables: Dict[str, Any],
        *,
        operation_name: str,
    ) -> Dict[str, Any]:
        """POST one GraphQL document and return its `data` object.

        Every attempt passes the process-wide rate gate first, so a job never
        outruns its siblings.

        Two retry budgets, deliberately separate:

        * transport failures and "the system is busy" spend
          `AD_API_MAX_RETRIES`;
        * `RateLimited` (`00:400998`) spends `AD_API_RATE_LIMIT_MAX_RETRIES`,
          which is larger, because it is the one failure where waiting
          reliably works. Letting a rate limit exhaust the transport budget
          would fail the job, and the requeued job restarts at `page_from` —
          spending *more* requests against the endpoint that just asked us to
          slow down. Waiting in place is strictly cheaper.

        `AuthExpired`, `PlanDenied` and `BadFilter` are raised at once; none
        of them can be fixed by trying again.
        """
        last_error: Optional[YouCloudError] = None
        # Read per call, not in __init__: config is meant to be live, and a
        # client constructed before a settings change should honour it.
        max_retries = max(1, settings.AD_API_MAX_RETRIES)
        rate_limit_retries = max(1, settings.AD_API_RATE_LIMIT_MAX_RETRIES)
        attempt = 0
        rate_limit_attempt = 0

        while True:
            cookie = await self._session_provider()
            if not cookie:
                # Missing, expired or locked-out — the provider already
                # logged which. The message is what lands on the job row, so
                # it names the fix rather than the symptom.
                raise AuthExpired(
                    "no usable YouCloud session token — paste a fresh one via " "PUT /api/v1/credentials/session"
                )

            payload = {"operationName": operation_name, "query": query, "variables": variables}

            waited = await self._throttle.acquire()
            if waited > 0:
                self._waited_seconds += waited
                ad_throttle_wait_seconds_total.inc(waited)
            ad_throttle_interval_seconds.set(self._throttle.interval)

            try:
                response = await self._http.post(
                    settings.AD_API_URL,
                    json=payload,
                    headers={"x-operation-name": operation_name},
                    cookies={"localeLanguage": settings.AD_API_LANGUAGE, "sessionId": cookie},
                )
            except httpx.HTTPError as exc:
                attempt += 1
                last_error = TransportError(f"transport failure: {exc}")
                ad_api_errors_total.labels(code="TransportError").inc()
                if attempt >= max_retries:
                    raise last_error from exc
                await self._backoff(attempt, "transport", str(exc))
                continue

            # The endpoint answers 200 for application errors, so a non-2xx
            # status means something upstream of the resolver — a stripped
            # header (406), a WAF, or a gateway. Body is plain text there.
            if not 200 <= response.status_code < 300:
                attempt += 1
                last_error = TransportError(
                    f"HTTP {response.status_code}: {response.text[:200]}",
                    code=f"http_{response.status_code}",
                )
                ad_api_errors_total.labels(code=f"http_{response.status_code}").inc()
                if attempt >= max_retries:
                    raise last_error
                await self._backoff(attempt, "http_status", str(response.status_code))
                continue

            try:
                body = response.json()
            except ValueError as exc:
                attempt += 1
                last_error = TransportError(f"non-JSON response: {response.text[:200]}", code="non_json")
                ad_api_errors_total.labels(code="non_json").inc()
                if attempt >= max_retries:
                    raise last_error from exc
                await self._backoff(attempt, "non_json", str(exc))
                continue

            error = classify(body)
            if error is None:
                data = body.get("data")
                if not isinstance(data, dict):
                    raise TransportError(f"response carried neither errors nor data: {str(body)[:200]}")
                # Only a clean response counts toward relaxing the gate, so a
                # burst of errors cannot walk the interval back down.
                self._throttle.record_success()
                ad_throttle_interval_seconds.set(self._throttle.interval)
                return data

            ad_api_errors_total.labels(code=metric_label(error)).inc()

            # Must precede the TransientError branch — RateLimited subclasses it.
            if isinstance(error, RateLimited):
                rate_limit_attempt += 1
                ad_rate_limited_total.inc()
                # Widens the gate AND pauses sibling jobs, not just this one.
                interval = self._throttle.penalise(reason="rate_limited")
                ad_throttle_interval_seconds.set(interval)
                last_error = error
                if rate_limit_attempt >= rate_limit_retries:
                    raise error
                logger.warning(
                    "ad_api_rate_limited",
                    attempt=rate_limit_attempt,
                    of=rate_limit_retries,
                    interval_seconds=round(interval, 3),
                    detail=str(error)[:200],
                )
                # No extra sleep here: `penalise` already pushed the shared
                # gate out by the cooldown, and the next loop iteration waits
                # on it. Sleeping again would double the pause.
                continue

            if isinstance(error, TransientError):
                attempt += 1
                last_error = error
                if attempt >= max_retries:
                    raise error
                await self._backoff(attempt, "transient", str(error))
                continue

            # AuthExpired / PlanDenied / BadFilter — terminal by design.
            raise error

    async def _backoff(self, attempt: int, reason: str, detail: str) -> None:
        """Sleep before the next attempt, with jitter.

        Jitter matters for the same reason it does in the throttle: two
        worker containers that fail on the same upstream blip would otherwise
        retry in lockstep and arrive together.
        """
        delay = 2 ** (attempt - 1)
        delay *= 1.0 + max(0.0, settings.AD_API_JITTER_RATIO) * random.random()
        logger.warning(
            "ad_api_retry", reason=reason, attempt=attempt, backoff_seconds=round(delay, 3), detail=detail[:200]
        )
        await asyncio.sleep(delay)

    # ------------------------------------------------------------------
    # materialList
    # ------------------------------------------------------------------

    async def material_list(self, variables: Dict[str, Any], *, page: int, order: str) -> Dict[str, Any]:
        """Fetch one page of `materialList`.

        Returns the raw `materialList` object: `{page, total, limit, data}`.
        """
        self.assert_page_within_ceiling(page)
        merged = dict(variables)
        merged["page"] = page
        merged["order"] = order
        data = await self.execute(MATERIAL_LIST_QUERY, merged, operation_name=MATERIAL_LIST_OPERATION)
        result = data.get("materialList")
        if not isinstance(result, dict):
            raise TransportError("materialList missing from response data")
        ad_pages_fetched_total.inc()
        return result

    async def paginate_materials(
        self,
        variables: Dict[str, Any],
        *,
        page_from: int = 1,
        page_to: Optional[int] = None,
        order: str = "max_dt_desc",
    ) -> AsyncIterator[Dict[str, Any]]:
        """Yield `(page_number, materialList_payload)` across a page window.

        Two stop conditions, and the second one is load-bearing:

        1. A page that returns no rows.
        2. `page * limit >= total` — because **past the end the API repeats
           the last page instead of returning empty.** Measured: a filter with
           `total: 26` answers pages 1, 2 and 3 with the identical 26 rows.
           Without this bound, `page_to=200` on that filter would spend 200
           requests to fetch 26 creatives 200 times over, and the job's
           `materials_seen` would report 5 200.

        Pacing is not this method's job any more: the process-wide gate in
        `execute` spaces every request, including the first of a job and
        requests made by other jobs running alongside. The per-page sleep
        that used to live here paced each job independently, so two
        concurrent jobs pushed twice as hard as configured.

        Note that `order=max_dt_desc` over a live feed means rows shift
        between requests, so the same material can still surface on two
        adjacent pages of a large result set; the upsert makes that harmless,
        but page count is not a row count.
        """
        last_page = page_to if page_to is not None else settings.AD_DEFAULT_PAGE_TO
        self.assert_page_within_ceiling(last_page)
        if page_from < 1:
            raise BadFilter(f"page_from must be >= 1, got {page_from}")
        if last_page < page_from:
            raise BadFilter(f"page_to ({last_page}) must be >= page_from ({page_from})")

        for page in range(page_from, last_page + 1):
            payload = await self.material_list(variables, page=page, order=order)
            yield page, payload

            rows = payload.get("data")
            if not isinstance(rows, list) or not rows:
                logger.info("ad_pagination_exhausted", page=page, reason="empty_page")
                return

            total = payload.get("total")
            if isinstance(total, int) and total >= 0:
                # `limit` is server-fixed; trust the payload's own value when
                # present so a server-side change doesn't silently break the
                # bound.
                limit = payload.get("limit")
                limit = limit if isinstance(limit, int) and limit > 0 else settings.AD_PAGE_SIZE
                if page * limit >= total:
                    logger.info(
                        "ad_pagination_exhausted",
                        page=page,
                        reason="reached_total",
                        total=total,
                        limit=limit,
                    )
                    return

    # ------------------------------------------------------------------
    # Guards
    # ------------------------------------------------------------------

    @staticmethod
    def assert_page_within_ceiling(page: int) -> None:
        """Reject pages the server will reject, without spending a request.

        `materialList` refuses `page > 200` with "Parameter error, please
        clear the filter and refresh". Since `limit` is server-fixed at 50,
        that is a hard 10 000-row ceiling per filter set. Going deeper means
        partitioning the filter space (date window, area, media, platform,
        keyword), not asking for more pages.
        """
        if not isinstance(page, int) or isinstance(page, bool):
            raise BadFilter(f"page must be an int, got {type(page).__name__}")
        if page < 1:
            raise BadFilter(f"page must be >= 1, got {page}")
        if page > settings.AD_MAX_PAGE:
            raise BadFilter(
                f"page {page} exceeds the API ceiling of {settings.AD_MAX_PAGE} "
                f"({settings.max_rows_per_filter_set} rows per filter set). "
                "Narrow the filter (date window, area, media, platform, keyword) and run several jobs."
            )
