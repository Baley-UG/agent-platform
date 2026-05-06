"""Tracked-targets router — stub for M1; real implementation lands in M7."""

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.v1.deps import require_api_key

router = APIRouter(dependencies=[Depends(require_api_key)])


@router.get("")
async def list_targets():
    """List tracked targets. (M7)"""
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, detail="implemented in M7")


@router.post("")
async def create_target():
    """Register a tracked target. (M7)"""
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, detail="implemented in M7")


@router.get("/{target_id}")
async def get_target(target_id: str):
    """Target detail. (M7)"""
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, detail="implemented in M7")


@router.patch("/{target_id}")
async def update_target(target_id: str):
    """Update cadence / filters / status. (M7)"""
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, detail="implemented in M7")


@router.post("/{target_id}/activate")
async def activate_target(target_id: str):
    """Approve a pending_review (auto-discovered) target. (M7)"""
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, detail="implemented in M7")


@router.post("/{target_id}/run-now")
async def run_target_now(target_id: str):
    """Enqueue an immediate scan. (M7)"""
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, detail="implemented in M7")
