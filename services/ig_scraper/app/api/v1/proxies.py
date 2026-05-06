"""Proxies router — stub for M1; real implementation lands in M2."""

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.v1.deps import require_api_key

router = APIRouter(dependencies=[Depends(require_api_key)])


@router.get("")
async def list_proxies():
    """List proxies. (M2)"""
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, detail="implemented in M2")


@router.post("")
async def create_proxy():
    """Add a proxy. (M2)"""
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, detail="implemented in M2")


@router.post("/{proxy_id}/test")
async def test_proxy(proxy_id: str):
    """Run a connectivity check against a proxy. (M2)"""
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, detail="implemented in M2")
