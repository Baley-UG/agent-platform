"""Jobs router — stub for M1; real implementation lands in M3.

Routes are wired to /api/v1/jobs and gated by the API key dependency so
the URL surface and auth contract are both already exercised in M1.
"""

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.v1.deps import require_api_key

router = APIRouter(dependencies=[Depends(require_api_key)])


@router.post("")
async def create_job():
    """Enqueue a scan job. (M3)"""
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, detail="implemented in M3")


@router.get("")
async def list_jobs():
    """List jobs. (M3)"""
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, detail="implemented in M3")


@router.get("/{job_id}")
async def get_job(job_id: str):
    """Job detail. (M3)"""
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, detail="implemented in M3")


@router.post("/{job_id}/cancel")
async def cancel_job(job_id: str):
    """Cancel a queued or running job. (M3)"""
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, detail="implemented in M3")


@router.post("/{job_id}/retry")
async def retry_job(job_id: str):
    """Re-enqueue a failed job. (M3)"""
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, detail="implemented in M3")
