"""Async HTTP client for HikerAPI.

Thin wrapper around httpx with:
  * `x-access-key` header injection
  * Tenacity-style retry on 5xx / network errors
  * Pagination helper (`paginate_chunks`) that auto-walks the cursor
  * Quota-aware error mapping (402/429 → ScraperResult outcomes)

We deliberately do NOT use the official `hikerapi` Python SDK — it
brings sync-only patterns and adds a dependency for endpoints we
already speak natively over httpx.
"""

import asyncio
from typing import Any, AsyncIterator, Dict, Iterable, Optional

import httpx

from app.core.config import settings
from app.core.logging import logger
from app.services import hikerapi_usage


class HikerAPIError(Exception):
    """Base for everything HikerAPI-related."""


class HikerAPIQuotaExceeded(HikerAPIError):
    """402 / 429 from HikerAPI — billing or rate limit."""


class HikerAPINotFound(HikerAPIError):
    """404 — resource not present on Instagram (deleted, private, etc.)."""


class HikerAPIClient:
    """Reusable async client. Construct once per scrape; close at end."""

    def __init__(self) -> None:
        if not settings.HIKERAPI_KEY:
            raise HikerAPIError(
                "HIKERAPI_KEY is not configured. Set it in .env (USE_HIKERAPI=true)."
            )
        # `trust_env=False` so HTTP_PROXY / HTTPS_PROXY / ALL_PROXY env
        # vars (which may exist for IG scraping via instagrapi) don't
        # leak into HikerAPI calls. HikerAPI is a different service,
        # provider-managed; their endpoint isn't behind our proxy.
        self._http = httpx.AsyncClient(
            base_url=settings.HIKERAPI_BASE_URL,
            timeout=settings.HIKERAPI_TIMEOUT_SECONDS,
            headers={
                "x-access-key": settings.HIKERAPI_KEY,
                "accept": "application/json",
                "user-agent": "ig-scraper/1.0 (HikerAPI client)",
            },
            trust_env=False,
        )
        self._max_retries = settings.HIKERAPI_MAX_RETRIES

    async def __aenter__(self) -> "HikerAPIClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self._http.aclose()

    async def get(self, path: str, **params: Any) -> Dict[str, Any]:
        """GET `path` with retry on 5xx / network errors.

        Auto-injects `privacy_check` from `HIKERAPI_PRIVACY_CHECK`
        config (default false) on every call so we don't get charged
        2x for the visibility check we don't use. Caller can override
        per-call by passing `privacy_check=True/False` explicitly.
        """
        # Drop None-valued params so the URL stays clean.
        clean_params = {k: v for k, v in params.items() if v is not None and v != ""}
        clean_params.setdefault(
            "privacy_check",
            "true" if settings.HIKERAPI_PRIVACY_CHECK else "false",
        )

        # Per-request log so we can audit HikerAPI billing/usage in Loki:
        #   {service="ig-scraper-worker"} |= "hikerapi_request" | json | path="/v1/user/medias/chunk"
        # Strips `privacy_check` from the logged params (it's an internal
        # default we set on every call) and trims long values.
        _log_params = {
            k: (str(v)[:120] if not isinstance(v, (int, float, bool)) else v)
            for k, v in clean_params.items()
            if k != "privacy_check"
        }
        logger.info("hikerapi_request", path=path, params=_log_params)

        for attempt in range(1, self._max_retries + 1):
            try:
                response = await self._http.get(path, params=clean_params)
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as exc:
                # Network failure has no HTTP status — record as 0 so it
                # still shows up in the per-day usage table.
                hikerapi_usage.record_call(path, 0)
                if attempt >= self._max_retries:
                    raise HikerAPIError(f"network error after {attempt} retries: {exc}") from exc
                backoff = 2 ** (attempt - 1)
                logger.warning(
                    "hikerapi_network_retry",
                    path=path,
                    attempt=attempt,
                    error=str(exc),
                    backoff_seconds=backoff,
                )
                await asyncio.sleep(backoff)
                continue

            status = response.status_code
            # Record the call BEFORE the status branching below. Even on
            # 402/429 the request was billed by HikerAPI, so it has to
            # land in the counter.
            hikerapi_usage.record_call(path, status)
            if 200 <= status < 300:
                try:
                    return response.json()
                except ValueError as exc:
                    raise HikerAPIError(f"non-JSON response from {path}: {exc}") from exc

            if status == 404:
                raise HikerAPINotFound(f"{path} returned 404")
            if status in (402, 403):
                raise HikerAPIQuotaExceeded(
                    f"{path} returned {status} — billing / access issue: {response.text[:200]}"
                )
            if status == 429:
                # Rate-limited — retry with backoff if we have attempts left.
                if attempt >= self._max_retries:
                    raise HikerAPIQuotaExceeded(
                        f"{path} rate-limited after {attempt} attempts"
                    )
                retry_after = int(response.headers.get("retry-after", "5"))
                logger.warning(
                    "hikerapi_rate_limit",
                    path=path,
                    attempt=attempt,
                    retry_after=retry_after,
                )
                await asyncio.sleep(retry_after)
                continue
            if 500 <= status < 600:
                if attempt >= self._max_retries:
                    raise HikerAPIError(
                        f"{path} returned {status} after {attempt} retries"
                    )
                backoff = 2 ** (attempt - 1)
                logger.warning(
                    "hikerapi_5xx_retry",
                    path=path,
                    status=status,
                    attempt=attempt,
                    backoff_seconds=backoff,
                )
                await asyncio.sleep(backoff)
                continue

            # 4xx that isn't 404 / 402 / 429 — likely a client mistake; surface it.
            raise HikerAPIError(
                f"{path} returned {status}: {response.text[:200]}"
            )

        raise HikerAPIError(f"{path} exhausted retries without a definitive response")

    # ------------------------------------------------------------------
    # Pagination
    # ------------------------------------------------------------------

    async def paginate_chunks(
        self,
        path: str,
        items_key: str,
        *,
        max_items: Optional[int] = None,
        stop_when: Optional[Any] = None,
        **params: Any,
    ) -> AsyncIterator[Dict[str, Any]]:
        """Yield items across HikerAPI cursor-paginated chunks.

        HikerAPI chunk endpoints return:
            {
              "<items_key>": [...],
              "next_max_id" or "end_cursor": "...",  # null when done
              ...
            }

        `stop_when(item) -> bool` lets the caller short-circuit (e.g.
        incremental cursor: stop on first known post). `max_items` caps
        total yields regardless.
        """
        cursor = None
        yielded = 0

        while True:
            page_params = dict(params)
            if cursor:
                page_params["end_cursor"] = cursor
            page = await self.get(path, **page_params)

            # HikerAPI returns two shapes depending on the endpoint:
            #   1. Object: {"<items_key>": [...], "next_max_id": "..."}
            #      (e.g. /v2/hashtag/medias/top, /v1/media/comments/chunk)
            #   2. Bare 2-tuple-as-array: [[item, item, ...], "next_cursor"]
            #      (e.g. /v1/user/medias/chunk, /v1/user/clips/chunk —
            #      most chunked v1 endpoints, where the second element is
            #      the cursor string or null).
            # We normalise both into (items, next_cursor) here so the
            # caller never has to think about it.
            items: list = []
            next_cursor: Optional[str] = None

            if isinstance(page, list):
                # Shape 2: [items, cursor]. Cursor is None/empty on the
                # last page. Some endpoints just return [items] without a
                # cursor element — treat that as "no more data".
                if page and isinstance(page[0], list):
                    items = page[0]
                    next_cursor = page[1] if len(page) > 1 and isinstance(page[1], str) else None
                else:
                    # Bare list of items, no cursor structure at all.
                    items = page
            elif isinstance(page, dict):
                # Shape 1. Try requested key first, then common fallbacks.
                candidate = page.get(items_key)
                if isinstance(candidate, list):
                    items = candidate
                else:
                    for fallback in ("response", "items", "medias", "comments", "stories"):
                        if fallback == items_key:
                            continue
                        candidate = page.get(fallback)
                        if isinstance(candidate, list):
                            items = candidate
                            break
                # HikerAPI uses different cursor field names depending on
                # the endpoint:
                #   - v1/*/chunk + most v2 paginated objects → end_cursor
                #   - v2/hashtag/medias/{top,recent}        → next_page_id
                #   - some legacy endpoints                 → next_max_id
                next_cursor = (
                    page.get("end_cursor")
                    or page.get("next_page_id")
                    or page.get("next_max_id")
                    or page.get("next_cursor")
                )

            for item in items:
                if stop_when is not None and stop_when(item):
                    return
                yield item
                yielded += 1
                if max_items is not None and yielded >= max_items:
                    return

            # Empty page or no cursor → no more data.
            if not items or not next_cursor:
                return
            cursor = next_cursor
