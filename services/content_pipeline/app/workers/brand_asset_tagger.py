"""RQ task — auto-tag a brand asset via vision LLM.

Entry point: `app.workers.brand_asset_tagger.run(asset_id)`.

Enqueued by:
  - `POST /brand-assets` on a fresh upload (when `auto_tag=true`)
  - `POST /brand-assets/{id}/retag` (admin manual retrigger)

Fail-open: any LLM/parsing error logs a warning and leaves the asset
untagged. The asset stays usable; admin can patch tags manually.
"""

from __future__ import annotations

import asyncio
import uuid

from app.core.logging import logger
from app.models.media_assets import MediaAsset
from app.models.projects import Project
from app.services import brand_asset_tagger as tagger_svc
from app.services import generation_calls as calls_svc
from app.services.database import session_scope


def run(asset_id: str) -> dict:
    asset_uuid = uuid.UUID(asset_id)

    with session_scope() as session:
        asset = session.get(MediaAsset, asset_uuid)
        if asset is None:
            logger.warning("brand_asset_tagger_missing", asset_id=asset_id)
            return {"ok": False, "error": "asset not found"}
        project = session.get(Project, asset.project_id)
        if project is None:
            logger.warning(
                "brand_asset_tagger_project_missing",
                asset_id=asset_id,
                project_id=str(asset.project_id),
            )
            return {"ok": False, "error": "project not found"}

        try:
            asset_type, tags, cost_usd, latency_ms = asyncio.run(
                tagger_svc.tag_asset(asset=asset, project=project, session=session)
            )
        except Exception as exc:  # noqa: BLE001
            # The tagger should never raise — wrap defensively so RQ
            # doesn't mark the job as failed just because the LLM call
            # blew up. Tagging failure is non-fatal.
            logger.warning(
                "brand_asset_tagger_unhandled",
                asset_id=asset_id,
                error=str(exc),
            )
            return {"ok": False, "error": str(exc)}

        if asset_type is None and tags is None:
            # Couldn't tag (no S3 bytes / no LLM route / parse failure).
            # Don't stamp auto_tagged_at — admin sees "Re-tag pending".
            return {"ok": False, "error": "tagger returned no usable result"}

        tagger_svc.apply_tagger_result(
            asset=asset, brand_asset_type=asset_type, brand_asset_tags=tags
        )
        session.add(asset)
        session.flush()

        if cost_usd is not None:
            # Ledger the call so the cost dashboard accounts for tagging
            # spend. We tag with task_key='brand_asset_tag' so admins
            # can break it out per project.
            calls_svc.record(
                session,
                project_id=project.id,
                scenario_id=None,
                variant_id=None,
                task_key="brand_asset_tag",
                provider="openrouter",
                model_id="(auto)",
                cost_usd=cost_usd,
                latency_ms=latency_ms or 0,
                status_="success",
            )

        return {
            "ok": True,
            "asset_id": asset_id,
            "brand_asset_type": asset_type,
            "tags": tags,
        }
