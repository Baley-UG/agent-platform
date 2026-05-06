"""Tracked-targets router — full CRUD + activate/pause/run-now (M7)."""

import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.v1.deps import require_api_key
from app.schemas.jobs import JobRead
from app.schemas.targets import TargetCreate, TargetRead, TargetUpdate
from app.services import targets as targets_service
from app.services.database import session_scope

router = APIRouter(dependencies=[Depends(require_api_key)])


@router.post("", response_model=TargetRead, status_code=status.HTTP_201_CREATED)
def create_target(payload: TargetCreate) -> TargetRead:
    """Register a tracked target. The scheduler will run it on the next tick."""
    try:
        with session_scope() as session:
            return targets_service.create_target(session, payload)
    except targets_service.TargetConflictError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc))
    except targets_service.InvalidTargetStateError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.get("", response_model=List[TargetRead])
def list_targets(
    kind: Optional[str] = Query(default=None),
    status_filter: Optional[str] = Query(default=None, alias="status"),
    auto_discovered: Optional[bool] = Query(default=None),
) -> List[TargetRead]:
    """Filtered list of tracked targets."""
    try:
        with session_scope() as session:
            return targets_service.list_targets(
                session,
                kind=kind,
                status=status_filter,
                auto_discovered=auto_discovered,
            )
    except targets_service.InvalidTargetStateError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.get("/{target_id}", response_model=TargetRead)
def get_target(target_id: uuid.UUID) -> TargetRead:
    """Fetch a single target."""
    try:
        with session_scope() as session:
            return targets_service.get_target(session, target_id)
    except targets_service.TargetNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="target not found")


@router.patch("/{target_id}", response_model=TargetRead)
def update_target(target_id: uuid.UUID, payload: TargetUpdate) -> TargetRead:
    """Update cadence / filters / status."""
    try:
        with session_scope() as session:
            return targets_service.update_target(session, target_id, payload)
    except targets_service.TargetNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="target not found")
    except targets_service.InvalidTargetStateError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.post("/{target_id}/activate", response_model=TargetRead)
def activate_target(target_id: uuid.UUID) -> TargetRead:
    """Flip a `pending_review` target to `active`."""
    try:
        with session_scope() as session:
            return targets_service.activate_target(session, target_id)
    except targets_service.TargetNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="target not found")


@router.post("/{target_id}/pause", response_model=TargetRead)
def pause_target(target_id: uuid.UUID) -> TargetRead:
    """Pause a target — it won't be enqueued by the scheduler."""
    try:
        with session_scope() as session:
            return targets_service.pause_target(session, target_id)
    except targets_service.TargetNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="target not found")


@router.post("/{target_id}/run-now", response_model=List[JobRead])
def run_target_now(target_id: uuid.UUID) -> List[JobRead]:
    """Enqueue the target's job(s) immediately, no cursor bump."""
    try:
        with session_scope() as session:
            jobs = targets_service.run_now(session, target_id)
            return [JobRead.model_validate(j, from_attributes=True) for j in jobs]
    except targets_service.TargetNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="target not found")
