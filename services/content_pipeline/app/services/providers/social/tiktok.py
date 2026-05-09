"""TikTok Content Posting API publisher.

The Content Posting API supports two upload modes:

- **PULL_FROM_URL**: TikTok pulls the video from a publicly fetchable URL
  (same model as IG Graph API). Single POST → polled status.
- **FILE_UPLOAD**: chunked PUT, more complex. We use PULL_FROM_URL for
  consistency with the IG path.

Endpoints (v2, https://open.tiktokapis.com/v2):

  POST /post/publish/video/init/
    body: {
      post_info: {title, privacy_level, disable_duet, disable_stitch,
                  disable_comment, video_cover_timestamp_ms},
      source_info: {source: "PULL_FROM_URL", video_url}
    }
    → {data: {publish_id}, error: {code, message}}

  POST /post/publish/status/fetch/
    body: {publish_id}
    → {data: {status: "PROCESSING_DOWNLOAD"|"PROCESSING_UPLOAD"
                       |"PROCESSING_PUBLISH"|"PUBLISH_COMPLETE"|"FAILED",
              fail_reason, publicaly_available_post_id?}, ...}

Auth: long-lived **OAuth user access token** stored in
`social_accounts.credentials_encrypted` as `{"access_token": "...",
"open_id": "..."}`.

Like IG, the `video_url` MUST be publicly fetchable by TikTok's CDN.
Same prod/dev tradeoff as the IG publisher.
"""

from __future__ import annotations

import asyncio
import time
from typing import Optional

import httpx

from app.core.logging import logger


_BASE_URL = "https://open.tiktokapis.com/v2"
_INITIAL_POLL_SECONDS = 4.0
_MAX_POLL_SECONDS = 30.0
_HARD_DEADLINE_SECONDS = 600.0


class TikTokPublishError(RuntimeError):
    pass


class TikTokPublisher:
    """Thin TikTok Content Posting API wrapper using the PULL_FROM_URL path."""

    def __init__(self, *, access_token: str, open_id: Optional[str] = None) -> None:
        if not access_token:
            raise TikTokPublishError("access_token is empty")
        self.access_token = access_token
        self.open_id = open_id or ""

    async def publish_video(
        self,
        *,
        public_video_url: str,
        title: Optional[str] = None,
        privacy_level: str = "MUTUAL_FOLLOW_FRIENDS",
        disable_duet: bool = False,
        disable_stitch: bool = False,
        disable_comment: bool = False,
        video_cover_timestamp_ms: int = 1000,
    ) -> dict:
        """Run the full init → poll → completed flow. Returns the final
        status payload, including `publicaly_available_post_id` when
        TikTok exposes one.
        """
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
            publish_id = await self._init_publish(
                client,
                payload={
                    "post_info": {
                        "title": title or "",
                        "privacy_level": privacy_level,
                        "disable_duet": disable_duet,
                        "disable_stitch": disable_stitch,
                        "disable_comment": disable_comment,
                        "video_cover_timestamp_ms": video_cover_timestamp_ms,
                    },
                    "source_info": {
                        "source": "PULL_FROM_URL",
                        "video_url": public_video_url,
                    },
                },
            )
            final = await self._wait_for_complete(client, publish_id)
            return {"publish_id": publish_id, **final}

    # ----- internal helpers -----

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json; charset=UTF-8",
        }

    async def _init_publish(self, client: httpx.AsyncClient, *, payload: dict) -> str:
        url = f"{_BASE_URL}/post/publish/video/init/"
        resp = await client.post(url, json=payload, headers=self._headers())
        if resp.status_code >= 400:
            logger.warning("tiktok_init_non_2xx", status=resp.status_code, body=resp.text[:1000])
            raise TikTokPublishError(f"init {resp.status_code}: {resp.text[:500]}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise TikTokPublishError(f"init returned non-JSON: {exc}") from exc
        err = (data.get("error") or {})
        if err.get("code") and err.get("code") != "ok":
            raise TikTokPublishError(f"init error: {err}")
        publish_id = (data.get("data") or {}).get("publish_id")
        if not publish_id:
            raise TikTokPublishError(f"init response missing publish_id: {data}")
        return publish_id

    async def _wait_for_complete(self, client: httpx.AsyncClient, publish_id: str) -> dict:
        deadline = time.monotonic() + _HARD_DEADLINE_SECONDS
        interval = _INITIAL_POLL_SECONDS
        url = f"{_BASE_URL}/post/publish/status/fetch/"
        while True:
            if time.monotonic() > deadline:
                raise TikTokPublishError(f"publish_id {publish_id} polling timed out")
            await asyncio.sleep(interval)
            interval = min(interval * 1.5, _MAX_POLL_SECONDS)
            try:
                resp = await client.post(url, json={"publish_id": publish_id}, headers=self._headers())
            except httpx.HTTPError as exc:
                logger.warning("tiktok_poll_transient", error=str(exc), publish_id=publish_id)
                continue
            if resp.status_code >= 500:
                logger.warning("tiktok_poll_5xx", status=resp.status_code, publish_id=publish_id)
                continue
            if resp.status_code >= 400:
                raise TikTokPublishError(f"poll {resp.status_code}: {resp.text[:500]}")
            try:
                data = resp.json()
            except ValueError:
                continue
            payload = data.get("data") or {}
            status_str = (payload.get("status") or "").upper()
            if status_str == "PUBLISH_COMPLETE":
                return payload
            if status_str == "FAILED":
                raise TikTokPublishError(f"publish failed: {payload}")
