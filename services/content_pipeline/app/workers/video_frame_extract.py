"""RQ task — extract keyframes from a brand video asset.

Entry point: `app.workers.video_frame_extract.run(asset_id)`.

Triggered:
  - Automatically when a brand asset upload completes with
    `mime_type` starting with `video/`.
  - Manually from the panel via `POST /brand-assets/{id}/extract-frames`
    (Phase 3 polish — to re-extract after admin edits the source video).

Each extracted frame becomes a fresh `media_assets` row tagged as
`brand_library` with the parent's `source_asset_id` and the
ffmpeg-discovered `source_timestamp_sec`. The vision auto-tagger
fires on each frame so the director sees mood/subjects/has_face for
free.

Runs in the **render container** because the generic worker image
doesn't ship ffmpeg.
"""

from __future__ import annotations

import os
import tempfile
import uuid

from app.core import s3 as s3lib
from app.core.config import settings
from app.core.logging import logger
from app.models.media_assets import MediaAsset
from app.models.projects import Project
from app.services import media_assets as media_svc
from app.services import queue as queue_svc
from app.services import video_frames as vf
from app.services.database import session_scope


def _download_video(asset: MediaAsset) -> str:
    """Pull the video bytes from S3 into /tmp and return the local path.

    Caller is responsible for cleanup — we use a tempfile that survives
    the function so ffmpeg can read it.
    """
    suffix = ".mp4"
    if asset.s3_key:
        _, ext = os.path.splitext(asset.s3_key)
        if ext:
            suffix = ext
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    try:
        s3lib.client().download_fileobj(settings.S3_BUCKET, asset.s3_key, tmp)
    finally:
        tmp.close()
    return tmp.name


def run(asset_id: str) -> dict:
    asset_uuid = uuid.UUID(asset_id)

    with session_scope() as session:
        asset = session.get(MediaAsset, asset_uuid)
        if asset is None:
            logger.warning("frame_extract_asset_missing", asset_id=asset_id)
            return {"ok": False, "error": "asset not found"}
        project = session.get(Project, asset.project_id)
        if project is None:
            return {"ok": False, "error": "project not found"}
        mime = (asset.mime_type or "").lower()
        if not mime.startswith("video/"):
            logger.info(
                "frame_extract_skipping_non_video",
                asset_id=asset_id,
                mime=mime,
            )
            return {"ok": False, "error": "not a video asset"}

        # Idempotency — if we already extracted frames for this asset
        # (panel-triggered second run is the only path that bypasses
        # this check), skip. We detect "already extracted" by the
        # existence of any child rows with `source_asset_id = asset.id`.
        from sqlmodel import select

        existing = session.exec(
            select(MediaAsset).where(MediaAsset.source_asset_id == asset.id).limit(1)
        ).first()
        if existing is not None:
            # Caller can force re-extract by deleting children first.
            logger.info(
                "frame_extract_already_done",
                asset_id=asset_id,
                first_child=str(existing.id),
            )
            return {"ok": True, "skipped": "already extracted"}

        # Pull bytes locally so ffmpeg can seek (avoids streaming
        # decode oddities with non-seekable inputs).
        try:
            local_path = _download_video(asset)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "frame_extract_download_failed",
                asset_id=asset_id,
                error=str(exc),
            )
            return {"ok": False, "error": f"download failed: {exc}"}

        try:
            frames = vf.extract_keyframes(local_path)
        except vf.FrameExtractError as exc:
            logger.warning(
                "frame_extract_ffmpeg_failed",
                asset_id=asset_id,
                error=str(exc),
            )
            try:
                os.unlink(local_path)
            except OSError:
                pass
            return {"ok": False, "error": str(exc)}

        if not frames:
            try:
                os.unlink(local_path)
            except OSError:
                pass
            logger.info("frame_extract_zero_frames", asset_id=asset_id)
            return {"ok": True, "frames": 0}

        # Upload + create media_assets rows. We tag each frame as
        # `brand_library` so the director's library query picks them up.
        # `brand_asset_type` is left null — the per-frame vision tagger
        # will fill it. `brand_kit_id` inherits from the parent so the
        # asset stays inside the same kit's pool.
        child_ids: list[str] = []
        for idx, frame in enumerate(frames):
            key = vf.s3_key_for_frame(project.id, asset.id, idx)
            try:
                s3lib.upload_bytes(key, frame.jpeg_bytes, content_type="image/jpeg")
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "frame_extract_upload_failed",
                    asset_id=asset_id,
                    idx=idx,
                    error=str(exc),
                )
                continue
            child = MediaAsset(
                project_id=project.id,
                type="brand_library",
                s3_key=key,
                mime_type="image/jpeg",
                size_bytes=len(frame.jpeg_bytes),
                source_asset_id=asset.id,
                source_timestamp_sec=frame.timestamp_sec,
                brand_kit_id=asset.brand_kit_id,
                status="ready",
            )
            session.add(child)
            session.flush()
            session.refresh(child)
            child_ids.append(str(child.id))

            # Fire-and-forget vision auto-tag for each new frame so it
            # joins the matchable library with full metadata.
            try:
                queue_svc.enqueue(
                    "brand_asset_tag",
                    "app.workers.brand_asset_tagger.run",
                    str(child.id),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "frame_extract_tag_enqueue_failed",
                    child_id=str(child.id),
                    error=str(exc),
                )

        # Stamp the parent so the panel can show "8 frames extracted"
        # without an extra query.
        meta = dict(asset.metadata_json or {})
        meta["frames_extracted"] = len(child_ids)
        meta["frames_extracted_at"] = (
            __import__("datetime").datetime.now().isoformat()
        )
        asset.metadata_json = meta
        session.add(asset)
        session.flush()

        try:
            os.unlink(local_path)
        except OSError:
            pass

        logger.info(
            "frame_extract_done",
            asset_id=asset_id,
            frames=len(child_ids),
        )
        return {
            "ok": True,
            "asset_id": asset_id,
            "frames": len(child_ids),
            "child_ids": child_ids,
        }


# Helper for FK targeting (kept here so the worker is self-contained).
_ = media_svc  # silence linter; future Phase 3.5 wires versioning helpers
