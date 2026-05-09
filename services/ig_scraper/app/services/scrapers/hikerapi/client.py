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

        for attempt in range(1, self._max_retries + 1):
            try:
                response = await self._http.get(path, params=clean_params)
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as exc:
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

            # HikerAPI's items live under different keys depending on the
        # endpoint — `medias`, `response`, `comments`, `items`. We try
        # the requested key first, then the common fallbacks. Returns
        # `[]` (empty page → loop ends) when none of them match.
        items = page.get(items_key)
        if not isinstance(items, list):
            for fallback in ("response", "items", "medias", "comments", "stories"):
                if fallback == items_key:
                    continue
                candidate = page.get(fallback)
                if isinstance(candidate, list):
                    items = candidate
                    break
        if not isinstance(items, list):
            items = []
            for item in items:
                if stop_when is not None and stop_when(item):
                    return
                yield item
                yielded += 1
                if max_items is not None and yielded >= max_items:
                    return

            # HikerAPI uses different cursor field names depending on the
            # endpoint shape; check the common ones.
            cursor = (
                page.get("end_cursor")
                or page.get("next_max_id")
                or page.get("next_cursor")
            )
            if not cursor:
                return
