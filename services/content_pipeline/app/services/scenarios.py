"""Scenarios CRUD + state machine + reuse policy enforcement.

State transitions guarded by `_ALLOWED_NEXT`. The analyzer worker bumps a
scenario from `analyzing` → `pending_review`; admin approve/edit/regenerate
endpoints drive the rest.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from fastapi import HTTPException, status
from sqlmodel import Session, select

from app.models.content_references import ContentReference
from app.models.projects import Project
from app.models.reference_usages import ReferenceUsage
from app.models.scenarios import SCENARIO_STATUSES, Scenario
from app.schemas.references import UsageCheck
from app.schemas.scenarios import ReuseConflict, ScenarioCreate, ScenarioUpdate
from app.services import references as references_svc


# ---------- aspect group mapping ----------

# Synced with PRESETS in PLAN § 4.1. CP-M3 will move this into a shared `presets.py`.
_VARIANT_ASPECT_GROUP = {
    "ig_reels": "9:16",
    "ig_story": "9:16",
    "tiktok": "9:16",
    "ig_shorts": "9:16",
    "ig_feed_45": "4:5",
    "ig_feed_11": "1:1",
}


def _derive_aspect_groups(target_variants: List[str]) -> List[str]:
    groups = []
    seen = set()
    for variant in target_variants:
        group = _VARIANT_ASPECT_GROUP.get(variant)
        if group and group not in seen:
            seen.add(group)
            groups.append(group)
    return groups


def scenario_source_kind(session: Session, scenario: Scenario) -> str:
    """Resolve the source media type for a scenario.

    Reads the linked content_reference's `metadata.media_type` /
    `metadata.product_type` and maps to one of:
      * `photo`    — single still image
      * `carousel` — multi-image slideshow
      * `reel`     — short vertical video (Instagram reels / clips)
      * `video`    — generic non-reel video
      * `unknown`  — no reference or no metadata; safe default `reel`

    Used by the pipeline to decide which generation steps to skip:
    photo/carousel sources don't need scene-video generation (Seedance)
    or voiceover (TTS) by default — the still images go straight to
    compose.
    """
    if scenario.reference_id is None:
        return "unknown"
    from app.models.content_references import ContentReference  # local import — avoid cycle

    ref = session.get(ContentReference, scenario.reference_id)
    if ref is None:
        return "unknown"
    meta = ref.metadata_json or {}
    product = (meta.get("product_type") or "").lower()
    if product in ("clips", "reels"):
        return "reel"
    media_type = meta.get("media_type")
    if media_type == 1:
        return "photo"
    if media_type == 8:
        return "carousel"
    if media_type == 2:
        return "video"
    return "unknown"


def pipeline_actions(session: Session, scenario: Scenario) -> dict:
    """Which "start_*" buttons should the admin panel show?

    Returns a flat dict of booleans keyed on action name. Driven by:
      1. Current scenario.status (state machine).
      2. Source kind (photo/carousel skip video + audio by default).

    Example flow for a photo source:
        approved → start_images → images_ready → start_compose →
        composing → final_pending_review → approve_final.

    Example flow for a reel source:
        approved → start_images → images_ready → start_videos →
        videos_ready → start_audio → audio_ready → start_compose →
        composing → final_pending_review → approve_final.
    """
    kind = scenario_source_kind(session, scenario)
    needs_video = kind in ("reel", "video", "unknown")
    needs_audio = needs_video  # voiceover only meaningful for video output
    status = scenario.status

    return {
        "source_kind": kind,
        "needs_video_generation": needs_video,
        "needs_audio_generation": needs_audio,
        "can_start_images": status == "approved",
        # Photo/carousel sources can skip straight from images_ready to
        # compose. Reel/video sources go through video + audio first.
        "can_start_videos": status == "images_ready" and needs_video,
        "can_start_audio": status == "videos_ready" and needs_audio,
        "can_start_compose": (
            (status == "images_ready" and not needs_video)
            or (status == "videos_ready" and not needs_audio)
            or status == "audio_ready"
        ),
        "can_approve_final": status == "final_pending_review",
    }


def derive_default_target_variants(reference) -> List[str]:
    """Pick a sensible default `target_variants` list based on the reference
    source. Used when the API caller omits `target_variants` — saves the
    admin panel from asking a question whose answer is implicit in the
    source post type.

    Mapping (ig_scraper.media_type, product_type) → variant:
        - clips / reels                    → ig_reels   (9:16 short video)
        - feed video / non-clip video      → ig_feed_45 (4:5 video post)
        - carousel                         → ig_feed_45 (4:5, most common
                                              chaton-style carousel ratio)
        - photo / feed photo               → ig_feed_45 (4:5 still post)
        - anything else / unknown          → ig_reels   (safe default —
                                              the most ubiquitous format)
    """
    meta = (getattr(reference, "metadata_json", None) or {}) if reference else {}
    media_type = meta.get("media_type")
    product_type = (meta.get("product_type") or "").lower()

    if product_type in ("clips", "reels"):
        return ["ig_reels"]
    if media_type == 8:  # carousel
        return ["ig_feed_45"]
    if media_type == 1:  # photo
        return ["ig_feed_45"]
    if media_type == 2:  # generic video (not a reel)
        return ["ig_feed_45"]
    return ["ig_reels"]


# ---------- state machine ----------

_ALLOWED_NEXT = {
    "draft": {"analyzing", "failed"},
    "analyzing": {"pending_review", "failed", "draft"},
    "pending_review": {"approved", "analyzing", "draft", "failed"},
    "approved": {"generating_images", "analyzing", "failed"},
    "generating_images": {"images_ready", "failed"},
    # `images_ready → composing` lets photo/carousel sources skip the
    # video and audio generation steps entirely; the static images go
    # straight to compose (single image OR slideshow). Reels still go
    # through generating_videos → generating_audio → composing.
    "images_ready": {"generating_videos", "composing", "failed"},
    "generating_videos": {"videos_ready", "failed"},
    # videos_ready can also re-enter generating_videos for scene-video regenerate.
    # `videos_ready → composing` lets reels skip the voiceover step when
    # the scenario has no narration (audio_mood: silent everywhere).
    "videos_ready": {"generating_audio", "composing", "generating_videos", "failed"},
    "generating_audio": {"audio_ready", "failed"},
    # audio_ready can re-enter generating_audio for voiceover regenerate.
    "audio_ready": {"composing", "generating_audio", "failed"},
    "composing": {"final_pending_review", "failed"},
    "final_pending_review": {"approved_final", "composing", "failed"},
    "approved_final": {"composing"},
    "failed": {"draft", "analyzing"},
}


class InvalidStateTransition(RuntimeError):
    """Raised when code tries to move a scenario between non-adjacent states."""


def transition(scenario: Scenario, new_status: str) -> None:
    """Check + apply a status transition. Raises if not allowed."""
    if new_status not in SCENARIO_STATUSES:
        raise InvalidStateTransition(f"unknown status: {new_status}")
    if scenario.status == new_status:
        return
    allowed = _ALLOWED_NEXT.get(scenario.status, set())
    if new_status not in allowed:
        raise InvalidStateTransition(f"cannot transition {scenario.status} → {new_status}")
    scenario.status = new_status


# ---------- reuse policy ----------


def _check_reuse(session: Session, project: Project, reference: ContentReference, force: bool) -> None:
    """Apply `projects.reuse_policy` to a fresh scenario create."""
    policy = project.reuse_policy
    if policy == "silent":
        return

    check: UsageCheck = references_svc.usage_check(session, project, reference.id)
    if not check.previously_used:
        return

    if policy == "block":
        # Block always denies, even with force=true.
        conflict = ReuseConflict(
            previously_used=True,
            usage_count=check.usage_count,
            last_used_days_ago=check.last_used_days_ago,
            previous_scenarios=check.previous_scenarios,
            project_reuse_policy=policy,
            hint="reuse_policy='block' on this project; reset to 'warn' to allow override",
        )
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=conflict.model_dump())

    # 'warn' — allow override only with force=true.
    if not force:
        conflict = ReuseConflict(
            previously_used=True,
            usage_count=check.usage_count,
            last_used_days_ago=check.last_used_days_ago,
            previous_scenarios=check.previous_scenarios,
            project_reuse_policy=policy,
        )
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=conflict.model_dump())


# ---------- CRUD ----------


def create(
    session: Session,
    project: Project,
    payload: ScenarioCreate,
    created_by: Optional[str] = None,
) -> Scenario:
    """Spawn a draft scenario. Doesn't run the analyzer — that's a follow-up enqueue."""
    reference = references_svc.get(session, project.id, payload.reference_id)
    _check_reuse(session, project, reference, force=payload.force)

    # Derive target_variants from the source if the caller didn't pin one.
    # Admin panel typically omits it — the reference's media_type implies
    # the natural target (reel→ig_reels, carousel→ig_feed_45, etc.).
    variants = list(payload.target_variants) if payload.target_variants else derive_default_target_variants(reference)

    # Seed publish-text fields from the reference so admins have a
    # starting point in the slot drawer instead of an empty caption.
    # Admin can later override per-slot via `plan_slots.caption_override`
    # OR per-scenario via PATCH (`scenario.default_caption`). The
    # publisher fall-through is slot.caption_override → scenario.default_caption.
    seeded_caption: Optional[str] = (reference.caption or "").strip() or None
    seeded_hashtags = list(reference.hashtags or []) or None

    scenario = Scenario(
        project_id=project.id,
        reference_id=reference.id,
        status="draft",
        target_variants=variants,
        target_aspect_groups=_derive_aspect_groups(variants),
        quality_tier=payload.quality_tier,
        production_mode=getattr(payload, "production_mode", "recreate"),
        default_caption=seeded_caption,
        default_hashtags=seeded_hashtags,
        created_by=created_by,
    )
    session.add(scenario)
    session.flush()
    session.refresh(scenario)

    usage = ReferenceUsage(
        project_id=project.id,
        reference_id=reference.id,
        scenario_id=scenario.id,
        status="produced",
        reuse_reason=payload.reuse_reason or "",
    )
    session.add(usage)
    session.flush()
    return scenario


def list_(
    session: Session,
    project_id: uuid.UUID,
    status_: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> List[Scenario]:
    stmt = select(Scenario).where(Scenario.project_id == project_id)
    if status_:
        stmt = stmt.where(Scenario.status == status_)
    stmt = stmt.order_by(Scenario.updated_at.desc()).limit(limit).offset(offset)
    return list(session.exec(stmt).all())


def get(session: Session, project_id: uuid.UUID, scenario_id: uuid.UUID) -> Scenario:
    row = session.get(Scenario, scenario_id)
    if row is None or row.project_id != project_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="scenario not found")
    return row


def update(
    session: Session, project_id: uuid.UUID, scenario_id: uuid.UUID, payload: ScenarioUpdate
) -> Scenario:
    row = get(session, project_id, scenario_id)
    data = payload.model_dump(exclude_unset=True)

    # Caption / hashtag edits are allowed in any state — admins set them
    # AFTER approval just before publishing. Pipeline-shape edits
    # (scenario_json, target_variants, quality_tier) only in draft / pending_review.
    pipeline_keys = {"scenario_json", "target_variants", "quality_tier"}
    if any(k in data for k in pipeline_keys) and row.status not in ("draft", "pending_review"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"scenario in status={row.status} cannot have its pipeline shape edited; regenerate or fail it first",
        )
    if "scenario_json" in data and data["scenario_json"] is not None:
        row.scenario_json = data["scenario_json"]
    if "target_variants" in data and data["target_variants"] is not None:
        row.target_variants = list(data["target_variants"])
        row.target_aspect_groups = _derive_aspect_groups(data["target_variants"])
    if "quality_tier" in data and data["quality_tier"] is not None:
        row.quality_tier = data["quality_tier"]
    if "default_caption" in data:
        row.default_caption = data["default_caption"]
    if "default_hashtags" in data:
        row.default_hashtags = list(data["default_hashtags"]) if data["default_hashtags"] is not None else None
    row.updated_at = datetime.now(timezone.utc)
    session.add(row)
    session.flush()
    session.refresh(row)
    return row


def approve(session: Session, project_id: uuid.UUID, scenario_id: uuid.UUID) -> Scenario:
    """Approve `pending_review` → `approved`.

    The image-generation fan-out (materializing `scene_renders` rows and
    transitioning to `generating_images`) is a separate action driven by
    `start_image_generation` so the admin can flip target_variants between
    approve and image-gen if they change their mind on platform mix.
    """
    row = get(session, project_id, scenario_id)
    if row.scenario_json is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="scenario_json is empty — run the analyzer or fill it manually before approving",
        )
    transition(row, "approved")
    session.add(row)
    session.flush()
    session.refresh(row)
    return row


def start_image_generation(session: Session, project_id: uuid.UUID, scenario_id: uuid.UUID) -> Scenario:
    """Move from `approved` to `generating_images`. Caller must materialize
    scene_renders + enqueue image_gen jobs (see `scene_renders` service).
    """
    row = get(session, project_id, scenario_id)
    if row.status != "approved":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"cannot start images from status={row.status}; approve the scenario first",
        )
    transition(row, "generating_images")
    session.add(row)
    session.flush()
    session.refresh(row)
    return row


def start_video_generation(session: Session, project_id: uuid.UUID, scenario_id: uuid.UUID) -> Scenario:
    """Move from `images_ready` to `generating_videos`. Caller must enqueue
    video_gen jobs for each scene_render that's `image_ready`.
    """
    row = get(session, project_id, scenario_id)
    if row.status != "images_ready":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"cannot start videos from status={row.status}; finish images_ready first",
        )
    transition(row, "generating_videos")
    session.add(row)
    session.flush()
    session.refresh(row)
    return row


def start_audio_generation(session: Session, project_id: uuid.UUID, scenario_id: uuid.UUID) -> Scenario:
    """Move from `videos_ready` (first run) or `audio_ready` (regenerate) to `generating_audio`."""
    row = get(session, project_id, scenario_id)
    if row.status not in ("videos_ready", "audio_ready"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"cannot start audio from status={row.status}; finish videos_ready first",
        )
    transition(row, "generating_audio")
    session.add(row)
    session.flush()
    session.refresh(row)
    return row


def mark_audio_ready(session: Session, scenario: Scenario) -> Scenario:
    transition(scenario, "audio_ready")
    session.add(scenario)
    session.flush()
    return scenario


def start_compose(session: Session, project_id: uuid.UUID, scenario_id: uuid.UUID) -> Scenario:
    """Move into `composing` from the earliest stage allowed for this scenario's source kind.

    Allowed entry statuses:
      * `images_ready` — photo/carousel sources (slideshow render, no video/audio stage).
      * `videos_ready` — silent reel (skips voiceover).
      * `audio_ready`  — full reel (voiceover already produced).
      * `final_pending_review` / `approved_final` — recompose after an edit.

    The `_ALLOWED_NEXT` transition map enforces the same set; this gate
    just produces a friendlier 409 message.
    """
    row = get(session, project_id, scenario_id)
    _COMPOSE_ENTRY_STATES = {
        "images_ready",
        "videos_ready",
        "audio_ready",
        "final_pending_review",
        "approved_final",
    }
    if row.status not in _COMPOSE_ENTRY_STATES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"cannot start compose from status={row.status}; "
                "reach images_ready (photo/carousel), videos_ready (silent reel), "
                "or audio_ready (full reel) first"
            ),
        )
    transition(row, "composing")
    session.add(row)
    session.flush()
    session.refresh(row)
    return row


def mark_final_pending_review(session: Session, scenario: Scenario) -> Scenario:
    transition(scenario, "final_pending_review")
    session.add(scenario)
    session.flush()
    return scenario


def approve_final(session: Session, project_id: uuid.UUID, scenario_id: uuid.UUID) -> Scenario:
    row = get(session, project_id, scenario_id)
    transition(row, "approved_final")
    session.add(row)
    session.flush()
    session.refresh(row)
    return row


def begin_regenerate(session: Session, project_id: uuid.UUID, scenario_id: uuid.UUID) -> Scenario:
    """Snapshot the current scenario_json into previous_*, bump version, mark analyzing.

    The actual regenerate work happens in the analyzer worker.
    """
    row = get(session, project_id, scenario_id)
    if row.status not in ("pending_review", "approved", "failed", "draft"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"scenario in status={row.status} cannot be regenerated from here",
        )
    if row.scenario_json is not None:
        row.previous_scenario_json = row.scenario_json
    row.version += 1
    row.last_error = None
    transition(row, "analyzing")
    session.add(row)
    session.flush()
    session.refresh(row)
    return row


def mark_analyzing(session: Session, scenario: Scenario) -> Scenario:
    transition(scenario, "analyzing")
    session.add(scenario)
    session.flush()
    return scenario


def mark_pending_review(session: Session, scenario: Scenario, scenario_json: dict) -> Scenario:
    scenario.scenario_json = scenario_json
    transition(scenario, "pending_review")
    session.add(scenario)
    session.flush()
    return scenario


def mark_failed(session: Session, scenario: Scenario, error: str) -> Scenario:
    scenario.last_error = error[:2000]
    transition(scenario, "failed")
    session.add(scenario)
    session.flush()
    return scenario


# ---------- bridge for the API to enqueue analyzer work ----------


def needs_analyzer(scenario: Scenario) -> bool:
    """True when the analyzer should be enqueued to (re)fill scenario_json."""
    return scenario.status in ("draft", "analyzing") and scenario.scenario_json is None
