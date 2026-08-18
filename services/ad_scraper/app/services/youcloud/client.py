"""Async HTTP client for the YouCloud/AppGrowing GraphQL endpoint.

Thin wrapper around httpx with:
  * the exact header set the endpoint validates (see below)
  * session-cookie injection from `ad_credentials`
  * body-based error classification (`app.services.youcloud.errors`)
  * `AuthExpired` surfaced immediately — only an operator can fix it
  * a page-ceiling guard that fails fast instead of round-tripping

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
from typing import Any, AsyncIterator, Dict, Optional

import httpx

from app.core.config import settings
from app.core.logging import logger
from app.core.metrics import ad_api_errors_total, ad_pages_fetched_total
from app.services.youcloud.errors import (
    AuthExpired,
    BadFilter,
    TransportError,
    TransientError,
    YouCloudError,
    classify,
    metric_label,
)
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
    """

    def __init__(self, *, session_provider) -> None:
        """Build a client over the injected session provider."""
        self._session_provider = session_provider
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
        self._max_retries = max(1, settings.AD_API_MAX_RETRIES)

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

        Raises the appropriate `YouCloudError` subclass on failure. Retries
        transport errors and `TransientError` with exponential backoff;
        `AuthExpired`, `PlanDenied` and `BadFilter` are raised at once —
        none of them can be fixed by trying again.
        """
        last_error: Optional[YouCloudError] = None

        for attempt in range(1, self._max_retries + 1):
            cookie = await self._session_provider()
            if not cookie:
                # Missing, expired or locked-out — the provider already
                # logged which. The message is what lands on the job row, so
                # it names the fix rather than the symptom.
                raise AuthExpired(
                    "no usable YouCloud session token — paste a fresh one via " "PUT /api/v1/credentials/session"
                )

            payload = {"operationName": operation_name, "query": query, "variables": variables}
            try:
                response = await self._http.post(
                    settings.AD_API_URL,
                    json=payload,
                    headers={"x-operation-name": operation_name},
                    cookies={"localeLanguage": settings.AD_API_LANGUAGE, "sessionId": cookie},
                )
            except httpx.HTTPError as exc:
                last_error = TransportError(f"transport failure: {exc}")
                ad_api_errors_total.labels(code="TransportError").inc()
                if attempt >= self._max_retries:
                    raise last_error from exc
                await self._backoff(attempt, "transport", str(exc))
                continue

            # The endpoint answers 200 for application errors, so a non-2xx
            # status means something upstream of the resolver — a stripped
            # header (406), a WAF, or a gateway. Body is plain text there.
            if not 200 <= response.status_code < 300:
                last_error = TransportError(
                    f"HTTP {response.status_code}: {response.text[:200]}",
                    code=f"http_{response.status_code}",
                )
                ad_api_errors_total.labels(code=f"http_{response.status_code}").inc()
                if attempt >= self._max_retries:
                    raise last_error
                await self._backoff(attempt, "http_status", str(response.status_code))
                continue

            try:
                body = response.json()
            except ValueError as exc:
                last_error = TransportError(f"non-JSON response: {response.text[:200]}", code="non_json")
                ad_api_errors_total.labels(code="non_json").inc()
                if attempt >= self._max_retries:
                    raise last_error from exc
                await self._backoff(attempt, "non_json", str(exc))
                continue

            error = classify(body)
            if error is None:
                data = body.get("data")
                if not isinstance(data, dict):
                    raise TransportError(f"response carried neither errors nor data: {str(body)[:200]}")
                return data

            ad_api_errors_total.labels(code=metric_label(error)).inc()

            if isinstance(error, TransientError):
                last_error = error
                if attempt >= self._max_retries:
                    raise error
                await self._backoff(attempt, "transient", str(error))
                continue

            # AuthExpired / PlanDenied / BadFilter — terminal by design.
            raise error

        raise last_error or TransportError("exhausted retries without a definitive response")

    async def _backoff(self, attempt: int, reason: str, detail: str) -> None:
        delay = 2 ** (attempt - 1)
        logger.warning("ad_api_retry", reason=reason, attempt=attempt, backoff_seconds=delay, detail=detail[:200])
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

        Stops early on the first page that returns no rows — the API pads
        rather than erroring past the end of a result set.

        A politeness delay separates pages. Note that `order=max_dt_desc`
        over a live feed means rows shift between requests, so the same
        material can surface on two pages; the upsert makes that harmless
        but it does mean page count is not a row count.
        """
        last_page = page_to if page_to is not None else settings.AD_DEFAULT_PAGE_TO
        self.assert_page_within_ceiling(last_page)
        if page_from < 1:
            raise BadFilter(f"page_from must be >= 1, got {page_from}")
        if last_page < page_from:
            raise BadFilter(f"page_to ({last_page}) must be >= page_from ({page_from})")

        for page in range(page_from, last_page + 1):
            if page > page_from and settings.AD_API_PAGE_DELAY_SECONDS > 0:
                await asyncio.sleep(settings.AD_API_PAGE_DELAY_SECONDS)
            payload = await self.material_list(variables, page=page, order=order)
            yield page, payload
            rows = payload.get("data")
            if not isinstance(rows, list) or not rows:
                logger.info("ad_pagination_exhausted", page=page)
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
