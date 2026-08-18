"""Main /api/v1 router."""

from fastapi import APIRouter

from app.api.v1.advertisers import router as advertisers_router
from app.api.v1.credentials import router as credentials_router
from app.api.v1.dimensions import router as dimensions_router
from app.api.v1.health import router as health_router
from app.api.v1.jobs import router as jobs_router
from app.api.v1.materials import router as materials_router

api_router = APIRouter()
api_router.include_router(health_router, tags=["health"])
api_router.include_router(credentials_router, prefix="/credentials", tags=["credentials"])
api_router.include_router(jobs_router, prefix="/jobs", tags=["jobs"])
api_router.include_router(materials_router, prefix="/materials", tags=["materials"])
api_router.include_router(advertisers_router, prefix="/advertisers", tags=["advertisers"])
api_router.include_router(dimensions_router, prefix="/dimensions", tags=["dimensions"])
