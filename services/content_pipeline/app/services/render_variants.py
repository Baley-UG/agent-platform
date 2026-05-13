"""render_variants service — fan-out across `scenario.target_variants`,
state rollup into the parent scenario, recompose support.
"""

from __future__ import annotations

import uuid
from typing import List, Optional

from fastapi import HTTPException, status
from sqlmodel import Session, select

from app.models.media_assets import MediaAsset
from app.models.render_variants import RenderVariant
from app.models.scenarios import Scenario
from app.services import scenarios as scenarios_svc


def materialize_for_scenario(session: Session, scenario: Scenario) -> List[RenderVariant]:
    """Insert one row per preset_key in `scenario.target_variants` (idempotent)."""
    presets = list(scenario.target_variants or [])
    if not presets:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="scenario has no target_variants"
        )

    existing = {
        v.preset_key
        for v in session.exec(
            select(RenderVariant).where(RenderVariant.scenario_id == scenario.id)
        ).all()
    }
    created: List[RenderVariant] = []
    for preset_key in presets:
        if preset_key in existing:
            continue
        row = RenderVariant(scenario_id=scenario.id, preset_key=preset_key, status="pending")
        session.add(row)
        created.append(row)
    if created:
        session.flush()
    return created


def list_for_scenario(session: Session, scenario_id: uuid.UUID) -> List[RenderVariant]:
    return list(
        session.exec(
            select(RenderVariant)
            .where(RenderVariant.scenario_id == scenario_id)
            .order_by(RenderVariant.preset_key)
        ).all()
    )


def get(session: Session, variant_id: uuid.UUID) -> RenderVariant:
    row = session.get(RenderVariant, variant_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="render_variant not found")
    return row


def get_for_preset(session: Session, scenario_id: uuid.UUID, preset_key: str) -> RenderVariant:
    stmt = select(RenderVariant).where(
        RenderVariant.scenario_id == scenario_id, RenderVariant.preset_key == preset_key
    )
    row = session.exec(stmt).first()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"render_variant not found for preset={preset_key}",
        )
    return row


# ----- worker callbacks -----


def mark_composing(session: Session, variant: RenderVariant) -> RenderVariant:
    variant.status = "composing"
    variant.error = None
    session.add(variant)
    session.flush()
    return variant


def mark_ready(
    session: Session,
    variant: RenderVariant,
    *,
    final_asset: MediaAsset,
    thumbnail_asset: Optional[MediaAsset] = None,
    duration_sec: Optional[float] = None,
    file_size_bytes: Optional[int] = None,
    render_recipe: Optional[dict] = None,
) -> RenderVariant:
    variant.final_asset_id = final_asset.id
    # Single-asset variants store a 1-element list so the
    # `final_asset_ids` field is always authoritative for downstream
    # carousel-aware consumers (publisher, panel).
    variant.final_asset_ids = [str(final_asset.id)]
    if thumbnail_asset is not None:
        variant.thumbnail_asset_id = thumbnail_asset.id
    if duration_sec is not None:
        variant.duration_sec = duration_sec
    if file_size_bytes is not None:
        variant.file_size_bytes = file_size_bytes
    if render_recipe is not None:
        variant.render_recipe = render_recipe
    variant.status = "ready"
    variant.error = None
    session.add(variant)
    session.flush()
    return variant


def mark_ready_carousel(
    session: Session,
    variant: RenderVariant,
    *,
    assets: List[MediaAsset],
    thumbnail_asset: Optional[MediaAsset] = None,
    render_recipe: Optional[dict] = None,
) -> RenderVariant:
    """Mark a multi-image (carousel post) variant ready.

    No single composite mp4 — we publish the per-slide images directly
    via Instagram's CAROUSEL_ALBUM endpoint. `final_asset_id` mirrors
    `assets[0].id` so legacy single-asset readers still get the cover
    image. Asset order is the publication order.
    """
    if not assets:
        raise ValueError("mark_ready_carousel requires at least one asset")
    variant.final_asset_id = assets[0].id
    variant.final_asset_ids = [str(a.id) for a in assets]
    if thumbnail_asset is not None:
        variant.thumbnail_asset_id = thumbnail_asset.id
    else:
        variant.thumbnail_asset_id = assets[0].id
    if render_recipe is not None:
        variant.render_recipe = render_recipe
    # Carousels have no single "duration"; leave fields at their
    # previous values. file_size_bytes likewise stays unset.
    variant.status = "ready"
    variant.error = None
    session.add(variant)
    session.flush()
    return variant


def mark_failed(session: Session, variant: RenderVariant, error: str) -> RenderVariant:
    variant.status = "failed"
    variant.error = error[:2000]
    session.add(variant)
    session.flush()
    return variant


def approve(session: Session, variant: RenderVariant) -> RenderVariant:
    if variant.status not in ("ready", "approved"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"cannot approve render_variant in status={variant.status}",
        )
    from datetime import datetime, timezone

    variant.status = "approved"
    variant.approved_at = datetime.now(timezone.utc)
    session.add(variant)
    session.flush()
    return variant


# ----- scenario rollup -----


def recompute_scenario_status_from_variants(session: Session, scenario: Scenario) -> Scenario:
    """When a render_variant finishes, advance the scenario.

    Rules:
    - Any variant `failed` AND scenario in `composing` → scenario `failed`.
    - All variants `ready` AND scenario in `composing` → `final_pending_review`.
    """
    rows = list_for_scenario(session, scenario.id)
    if not rows:
        return scenario

    statuses = {v.status for v in rows}

    if "failed" in statuses and scenario.status == "composing":
        scenarios_svc.mark_failed(
            session, scenario, "one or more render_variants failed (see render_variants.error)"
        )
        return scenario

    if scenario.status == "composing" and all(s in ("ready", "approved") for s in statuses):
        scenarios_svc.mark_final_pending_review(session, scenario)

    return scenario


def claim_for_recompose(session: Session, variant_id: uuid.UUID) -> RenderVariant:
    """Pre-flight for `POST /variants/{id}/recompose`."""
    variant = get(session, variant_id)
    if variant.status == "composing":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="render_variant is already composing; wait or fail it first",
        )
    return variant
