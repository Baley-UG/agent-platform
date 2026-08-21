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
    parent_remake_id: Optional[uuid.UUID] = None,
    source_timestamp_sec: Optional[float] = None,
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
        parent_remake_id=parent_remake_id,
        source_timestamp_sec=source_timestamp_sec,
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


def walk_chain(session: Session, asset_id: uuid.UUID) -> list[MediaAsset]:
    """Return the full version chain (oldest → newest) starting from any
    asset in the chain.

    We may be handed any version (the active one, a stale one mid-chain, or
    even the v1 root). We walk back to v1 via `previous_version_id`, then
    forward via `replaced_by_id`. Cycles are guarded by an id-set check —
    a corrupted chain returns what we managed to walk before the cycle,
    not an infinite loop.
    """
    seed = session.get(MediaAsset, asset_id)
    if seed is None:
        return []

    # Walk back to the root (v1).
    root = seed
    seen = {root.id}
    while root.previous_version_id is not None:
        prior = session.get(MediaAsset, root.previous_version_id)
        if prior is None or prior.id in seen:
            break
        seen.add(prior.id)
        root = prior

    # Walk forward to the active version.
    chain: list[MediaAsset] = [root]
    forward_seen = {root.id}
    cursor = root
    while cursor.replaced_by_id is not None:
        nxt = session.get(MediaAsset, cursor.replaced_by_id)
        if nxt is None or nxt.id in forward_seen:
            break
        forward_seen.add(nxt.id)
        chain.append(nxt)
        cursor = nxt
    return chain


def active_version(session: Session, asset_id: uuid.UUID) -> Optional[MediaAsset]:
    """Resolve any chain member to the currently-active (replaced_by_id IS NULL) version."""
    chain = walk_chain(session, asset_id)
    return chain[-1] if chain else None
