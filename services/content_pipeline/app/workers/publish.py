"""publish RQ task — drive a single plan_slot's publish_job to completion.

Entry point: `app.workers.publish.run(plan_slot_id, force_now=False)`.

Loads the slot's variant + final_asset_id, presigns a long-lived public
URL (or uses the cdn URL when configured), invokes the matching social
publisher, and records the outcome on `publish_jobs` + the parent slot.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Optional

from app.core import s3
from app.core.logging import logger
from app.models.media_assets import MediaAsset
from app.models.plan_slots import PlanSlot
from app.models.publish_jobs import PublishJob
from app.models.render_variants import RenderVariant
from app.services import publishing as svc
from app.services.database import session_scope
from app.services.providers.social.instagram import (
    InstagramPublisher,
    InstagramPublishError,
    variant_to_ig_media_type,
)


def _build_publisher(provider: str, credentials: dict):
    if provider == "instagram":
        return InstagramPublisher(
            access_token=credentials.get("access_token", ""),
            ig_user_id=credentials.get("ig_user_id", ""),
        )
    # CP-M7 will plug in tiktok here.
    raise NotImplementedError(f"social publisher not implemented yet: {provider}")


def _public_url_for_asset(asset: MediaAsset) -> str:
    """Return a URL Meta/TikTok can fetch.

    Strategy:
    - If the bucket policy is public (Hetzner with public read), `public_url`
      is fine and stable.
    - Otherwise, use a presigned GET with a long TTL (24h) so Meta has time
      to finish processing before the URL expires.
    """
    try:
        return s3.public_url(asset.s3_key)
    except Exception:  # noqa: BLE001
        return s3.presigned_get_url(asset.s3_key, ttl=86400)


def run(plan_slot_id: str, force_now: bool = False) -> dict:  # noqa: ARG001
    slot_uuid = uuid.UUID(plan_slot_id)

    with session_scope() as session:
        slot = session.get(PlanSlot, slot_uuid)
        if slot is None:
            logger.warning("publish_slot_missing", plan_slot_id=plan_slot_id)
            return {"ok": False, "error": "plan_slot not found"}

        if slot.variant_id is None:
            return {"ok": False, "error": "slot has no variant"}
        if slot.social_account_id is None:
            return {"ok": False, "error": "slot has no social_account"}

        variant = session.get(RenderVariant, slot.variant_id)
        if variant is None or variant.final_asset_id is None:
            slot.last_error = "variant missing or no final_asset_id"
            session.add(slot)
            session.flush()
            return {"ok": False, "error": "variant or final asset missing"}

        asset = session.get(MediaAsset, variant.final_asset_id)
        if asset is None:
            return {"ok": False, "error": "final media_asset missing"}

        # Reuse an existing pending job if one is already attached (retry path),
        # otherwise create a new one.
        job: Optional[PublishJob] = None
        if slot.publish_job_id is not None:
            job = session.get(PublishJob, slot.publish_job_id)
            if job is not None and job.status == "published":
                return {"ok": True, "already_published": True, "publish_job_id": str(job.id)}
        if job is None:
            job = svc.create_pending(session, slot)

        # Decrypt credentials.
        from app.models.social_accounts import SocialAccount

        account = session.get(SocialAccount, slot.social_account_id)
        if account is None:
            svc.mark_failed(session, job, "social_account missing")
            return {"ok": False, "error": "social_account missing"}
        creds = svc.get_credentials(session, account)

        try:
            publisher = _build_publisher(account.provider, creds)
        except (InstagramPublishError, NotImplementedError) as exc:
            svc.mark_failed(session, job, str(exc))
            slot.status = "failed"
            slot.last_error = str(exc)[:1000]
            session.add(slot)
            session.flush()
            return {"ok": False, "error": str(exc)}

        slot.status = "publishing"
        session.add(slot)
        svc.mark_uploading(session, job)

        public_url = _public_url_for_asset(asset)

        try:
            if account.provider == "instagram":
                response = asyncio.run(
                    publisher.publish_video(
                        public_video_url=public_url,
                        caption=(slot.last_error or "")[:0],  # placeholder; admin-supplied caption is CP-M6.5
                        media_type=variant_to_ig_media_type(slot.variant_preset),
                    )
                )
            else:
                response = {}
        except Exception as exc:  # noqa: BLE001
            logger.warning("publish_call_failed", plan_slot_id=plan_slot_id, error=str(exc))
            svc.mark_failed(session, job, str(exc))
            slot.status = "failed"
            slot.last_error = str(exc)[:1000]
            session.add(slot)
            session.flush()
            return {"ok": False, "error": str(exc)}

        media_id = (response or {}).get("id")
        svc.mark_published(session, job, media_id=media_id, response=response or {})
        slot.status = "published"
        session.add(slot)
        session.flush()

        return {
            "ok": True,
            "publish_job_id": str(job.id),
            "media_id": media_id,
            "provider": account.provider,
        }
