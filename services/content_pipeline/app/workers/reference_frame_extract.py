"""RQ task — extract keyframes from a reel reference.

Entry point: `app.workers.reference_frame_extract.run(reference_id)`.

Distinct from the brand-asset extractor:
- Reads from `content_references.media_s3_key` (the mirrored reel video).
- Writes frame keys to `content_references.metadata.frame_s3_keys[]`
  (an in-place metadata array; the panel + materialize_scene_renders
  read this directly).
- Does NOT create separate `media_assets` rows — reference frames are
  ephemeral, scoped to the parent reference.

Runs in the **render container** (ffmpeg-bound, like
`video_frame_extract`).
"""

from __future__ import annotations

import os
import tempfile
import uuid

from app.core import s3 as s3lib
from app.core.config import settings
from app.core.logging import logger
from app.models.content_references import ContentReference
from app.services import video_frames as vf
from app.services.database import session_scope


def _download_video(reference: ContentReference) -> str:
    """Pull the reel bytes from S3 into /tmp. Caller cleans up."""
    suffix = ".mp4"
    if reference.media_s3_key:
        _, ext = os.path.splitext(reference.media_s3_key)
        if ext:
            suffix = ext
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    try:
        s3lib.client().download_fileobj(
            settings.S3_BUCKET, reference.media_s3_key, tmp
        )
    finally:
        tmp.close()
    return tmp.name


def _s3_key_for_frame(reference_id: uuid.UUID, idx: int) -> str:
    return f"references/{reference_id}/frames/{idx:02d}.jpg"


def run(reference_id: str) -> dict:
    ref_uuid = uuid.UUID(reference_id)

    with session_scope() as session:
        ref = session.get(ContentReference, ref_uuid)
        if ref is None:
            logger.warning("ref_frame_extract_missing", reference_id=reference_id)
            return {"ok": False, "error": "reference not found"}

        if not ref.media_s3_key:
            return {"ok": False, "error": "reference has no mirrored video"}

        meta = dict(ref.metadata_json or {})
        existing = meta.get("frame_s3_keys") or []
        if isinstance(existing, list) and existing:
            # Already extracted — admins can force a re-run by editing
            # the reference and clearing `frame_s3_keys` (or via a
            # future panel button).
            return {"ok": True, "skipped": "already extracted", "frames": len(existing)}

        try:
            local_path = _download_video(ref)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "ref_frame_extract_download_failed",
                reference_id=reference_id,
                error=str(exc),
            )
            return {"ok": False, "error": f"download failed: {exc}"}

        try:
            frames = vf.extract_keyframes(local_path)
        except vf.FrameExtractError as exc:
            logger.warning(
                "ref_frame_extract_ffmpeg_failed",
                reference_id=reference_id,
                error=str(exc),
            )
            _safe_unlink(local_path)
            return {"ok": False, "error": str(exc)}

        if not frames:
            _safe_unlink(local_path)
            return {"ok": True, "frames": 0}

        # Upload + collect keys. We store flat keys + timestamps so
        # `compute_init_keys` can pick frames by scene index.
        frame_records: list[dict] = []
        for idx, fr in enumerate(frames):
            key = _s3_key_for_frame(ref.id, idx)
            try:
                s3lib.upload_bytes(key, fr.jpeg_bytes, content_type="image/jpeg")
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "ref_frame_extract_upload_failed",
                    reference_id=reference_id,
                    idx=idx,
                    error=str(exc),
                )
                continue
            frame_records.append({"s3_key": key, "timestamp_sec": fr.timestamp_sec})

        meta["frame_s3_keys"] = [r["s3_key"] for r in frame_records]
        meta["frame_records"] = frame_records  # keeps timestamps for future segment cut
        ref.metadata_json = meta
        session.add(ref)
        session.flush()

        _safe_unlink(local_path)

        logger.info(
            "ref_frame_extract_done",
            reference_id=reference_id,
            frames=len(frame_records),
        )
        return {
            "ok": True,
            "reference_id": reference_id,
            "frames": len(frame_records),
        }


def _safe_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass
