"""Helpers for the `media_assets` versioned chain.

Two patterns:
- `create_initial(...)`: first version (1) of an artifact. No previous link.
- `replace(...)`: bumps the prior asset's `replaced_by_id`, inserts a new
  row with `version = prior.version + 1` and `previous_version_id = prior.id`,
  and returns the new active row.

The "currently active" asset for a (scene_render or scenario, type) is the
latest row with `replaced_by_id IS NULL`. Rollback = swap `replaced_by_id`
links; never delete.
"""

from __future__ import annotations

import uuid
from typing import Optional

from sqlmodel import Session

from app.models.media_assets import MediaAsset


def create_initial(
    session: Session,
    *,
    project_id: uuid.UUID,
    type_: str,
    s3_key: str,
    mime_type: Optional[str] = None,
    size_bytes: Optional[int] = None,
    width: Optional[int] = None,
    height: Optional[int] = None,
    duration_sec: Optional[float] = None,
    parent_scenario_id: Optional[uuid.UUID] = None,
    parent_scene_idx: Optional[int] = None,
    metadata: Optional[dict] = None,
) -> MediaAsset:
    asset = MediaAsset(
        project_id=project_id,
        type=type_,
        s3_key=s3_key,
        mime_type=mime_type,
        size_bytes=size_bytes,
        width=width,
        height=height,
        duration_sec=duration_sec,
        parent_scenario_id=parent_scenario_id,
        parent_scene_idx=parent_scene_idx,
        metadata_json=metadata,
        version=1,
        previous_version_id=None,
        replaced_by_id=None,
        status="ready",
    )
    session.add(asset)
    session.flush()
    session.refresh(asset)
    return asset


def replace(
    session: Session,
    prior: MediaAsset,
    *,
    s3_key: str,
    mime_type: Optional[str] = None,
    size_bytes: Optional[int] = None,
    width: Optional[int] = None,
    height: Optional[int] = None,
    duration_sec: Optional[float] = None,
    metadata: Optional[dict] = None,
) -> MediaAsset:
    """Insert a new version pointing at `prior`; mark `prior.replaced_by_id`."""
    new_asset = MediaAsset(
        project_id=prior.project_id,
        type=prior.type,
        s3_key=s3_key,
        mime_type=mime_type or prior.mime_type,
        size_bytes=size_bytes,
        width=width,
        height=height,
        duration_sec=duration_sec,
        parent_scenario_id=prior.parent_scenario_id,
        parent_scene_idx=prior.parent_scene_idx,
        metadata_json=metadata,
        version=prior.version + 1,
        previous_version_id=prior.id,
        replaced_by_id=None,
        status="ready",
    )
    session.add(new_asset)
    session.flush()
    session.refresh(new_asset)

    prior.replaced_by_id = new_asset.id
    session.add(prior)
    session.flush()
    return new_asset


def get(session: Session, asset_id: uuid.UUID) -> Optional[MediaAsset]:
    return session.get(MediaAsset, asset_id)
