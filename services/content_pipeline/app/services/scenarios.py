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


# ---------- state machine ----------

_ALLOWED_NEXT = {
    "draft": {"analyzing", "failed"},
    "analyzing": {"pending_review", "failed", "draft"},
    "pending_review": {"approved", "analyzing", "draft", "failed"},
    "approved": {"generating_images", "analyzing", "failed"},
    "generating_images": {"images_ready", "failed"},
    "images_ready": {"generating_videos", "failed"},
    "generating_videos": {"videos_ready", "failed"},
    "videos_ready": {"generating_audio", "failed"},
    "generating_audio": {"audio_ready", "failed"},
    "audio_ready": {"composing", "failed"},
    "composing": {"final_pending_review", "failed"},
    "final_pending_review": {"approved_final", "composing", "failed"},
    "approved_final": set(),
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

    scenario = Scenario(
        project_id=project.id,
        reference_id=reference.id,
        status="draft",
        target_variants=list(payload.target_variants),
        target_aspect_groups=_derive_aspect_groups(payload.target_variants),
        quality_tier=payload.quality_tier,
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
    if row.status not in ("draft", "pending_review"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"scenario in status={row.status} cannot be edited; regenerate or fail it first",
        )
    data = payload.model_dump(exclude_unset=True)
    if "scenario_json" in data and data["scenario_json"] is not None:
        row.scenario_json = data["scenario_json"]
    if "target_variants" in data and data["target_variants"] is not None:
        row.target_variants = list(data["target_variants"])
        row.target_aspect_groups = _derive_aspect_groups(data["target_variants"])
    if "quality_tier" in data and data["quality_tier"] is not None:
        row.quality_tier = data["quality_tier"]
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
