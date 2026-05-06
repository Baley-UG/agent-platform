"""Accounts router — stub for M1; real implementation lands in M2."""

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.v1.deps import require_api_key

router = APIRouter(dependencies=[Depends(require_api_key)])


@router.get("")
async def list_accounts():
    """List scraping accounts. (M2)"""
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, detail="implemented in M2")


@router.post("")
async def create_account():
    """Register a scraping account (server encrypts credentials). (M2)"""
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, detail="implemented in M2")


@router.post("/{account_id}/disable")
async def disable_account(account_id: str):
    """Mark an account disabled. (M2)"""
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, detail="implemented in M2")
