"""Instagram Graph API publisher.

The Graph API publish flow is two-step:

1. Create a media container:
   POST https://graph.facebook.com/v22.0/{ig_user_id}/media
     body: { media_type: "REELS"|"VIDEO"|"STORIES"|..., video_url, caption, ... }
   → { id: <container_id> }

2. Poll container status until it's FINISHED:
   GET https://graph.facebook.com/v22.0/{container_id}?fields=status_code,status
   → status_code in {IN_PROGRESS, FINISHED, ERROR, EXPIRED, PUBLISHED}

3. Publish the container:
   POST https://graph.facebook.com/v22.0/{ig_user_id}/media_publish
     body: { creation_id: <container_id> }
   → { id: <media_id> }

For images, step 1 uses image_url instead of video_url and media_type
is omitted (or set to IMAGE for clarity).

The `video_url` / `image_url` MUST be publicly fetchable by Meta — that's
why the renderer keeps finals on the (Hetzner) public bucket. In dev with
MinIO, IG can't reach the URL; this provider works in prod.

Auth: long-lived page access token stored in
`social_accounts.credentials_encrypted` as `{"access_token": "...", "ig_user_id": "..."}`.
"""

from __future__ import annotations

import asyncio
import time
from typing import Optional

import httpx

from app.core.logging import logger


GRAPH_BASE = "https://graph.facebook.com/v22.0"
_INITIAL_POLL_SECONDS = 3.0
_MAX_POLL_SECONDS = 30.0
_HARD_DEADLINE_SECONDS = 600.0


class InstagramPublishError(RuntimeError):
    pass


class InstagramPublisher:
    """Thin Graph API wrapper for the publish flow."""

    def __init__(self, *, access_token: str, ig_user_id: str) -> None:
        if not access_token:
            raise InstagramPublishError("access_token is empty")
        if not ig_user_id:
            raise InstagramPublishError("ig_user_id is empty")
        self.access_token = access_token
        self.ig_user_id = ig_user_id

    async def publish_video(
        self,
        *,
        public_video_url: str,
        caption: Optional[str] = None,
        media_type: str = "REELS",
        share_to_feed: bool = False,
        thumb_offset_ms: Optional[int] = None,
    ) -> dict:
        """Run the full create → poll → publish flow. Returns the final
        Graph response payload, including `id` (the media id).
        """
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
            container_id = await self._create_container(
                client,
                payload={
                    "media_type": media_type,
                    "video_url": public_video_url,
                    "caption": caption or "",
                    "share_to_feed": "true" if share_to_feed else "false",
                    **({"thumb_offset": thumb_offset_ms} if thumb_offset_ms is not None else {}),
                    "access_token": self.access_token,
                },
            )
            await self._wait_for_finished(client, container_id)
            return await self._publish_container(client, container_id)

    async def publish_image(self, *, public_image_url: str, caption: Optional[str] = None) -> dict:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
            container_id = await self._create_container(
                client,
                payload={
                    "image_url": public_image_url,
                    "caption": caption or "",
                    "access_token": self.access_token,
                },
            )
            await self._wait_for_finished(client, container_id)
            return await self._publish_container(client, container_id)

    # ----- internal helpers -----

    async def _create_container(self, client: httpx.AsyncClient, *, payload: dict) -> str:
        url = f"{GRAPH_BASE}/{self.ig_user_id}/media"
        resp = await client.post(url, data=payload)
        if resp.status_code >= 400:
            logger.warning("ig_create_container_non_2xx", status=resp.status_code, body=resp.text[:1000])
            raise InstagramPublishError(f"create container {resp.status_code}: {resp.text[:500]}")
        try:
            data = resp.json()
            return data["id"]
        except (KeyError, ValueError) as exc:
            raise InstagramPublishError(f"create container response missing id: {resp.text[:500]}") from exc

    async def _wait_for_finished(self, client: httpx.AsyncClient, container_id: str) -> None:
        deadline = time.monotonic() + _HARD_DEADLINE_SECONDS
        interval = _INITIAL_POLL_SECONDS
        while True:
            if time.monotonic() > deadline:
                raise InstagramPublishError(f"container {container_id} polling timed out")
            await asyncio.sleep(interval)
            interval = min(interval * 1.5, _MAX_POLL_SECONDS)
            url = f"{GRAPH_BASE}/{container_id}?fields=status_code,status&access_token={self.access_token}"
            try:
                resp = await client.get(url)
            except httpx.HTTPError as exc:
                logger.warning("ig_poll_transient", error=str(exc), container_id=container_id)
                continue
            if resp.status_code >= 400:
                raise InstagramPublishError(f"poll {resp.status_code}: {resp.text[:500]}")
            try:
                data = resp.json()
            except ValueError:
                continue
            code = (data.get("status_code") or "").upper()
            if code in ("FINISHED", "PUBLISHED"):
                return
            if code in ("ERROR", "EXPIRED"):
                raise InstagramPublishError(f"container {container_id} failed: {data}")

    async def _publish_container(self, client: httpx.AsyncClient, container_id: str) -> dict:
        url = f"{GRAPH_BASE}/{self.ig_user_id}/media_publish"
        resp = await client.post(url, data={"creation_id": container_id, "access_token": self.access_token})
        if resp.status_code >= 400:
            logger.warning("ig_publish_non_2xx", status=resp.status_code, body=resp.text[:1000])
            raise InstagramPublishError(f"publish {resp.status_code}: {resp.text[:500]}")
        try:
            return resp.json()
        except ValueError as exc:
            raise InstagramPublishError(f"publish returned non-JSON: {exc}") from exc


_VARIANT_TO_IG_MEDIA_TYPE = {
    "ig_reels": "REELS",
    "ig_story": "STORIES",
    "ig_feed_45": "VIDEO",
    "ig_feed_11": "VIDEO",
}


def variant_to_ig_media_type(variant_preset: str) -> str:
    return _VARIANT_TO_IG_MEDIA_TYPE.get(variant_preset, "VIDEO")
