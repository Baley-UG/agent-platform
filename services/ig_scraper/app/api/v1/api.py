"""Main /api/v1 router."""

from fastapi import APIRouter

from app.api.v1.accounts import router as accounts_router
from app.api.v1.health import router as health_router
from app.api.v1.jobs import router as jobs_router
from app.api.v1.posts import router as posts_router
from app.api.v1.proxies import router as proxies_router
from app.api.v1.targets import router as targets_router
from app.api.v1.webhooks import router as webhooks_router

api_router = APIRouter()
api_router.include_router(health_router, tags=["health"])
api_router.include_router(jobs_router, prefix="/jobs", tags=["jobs"])
api_router.include_router(accounts_router, prefix="/accounts", tags=["accounts"])
api_router.include_router(proxies_router, prefix="/proxies", tags=["proxies"])
api_router.include_router(targets_router, prefix="/targets", tags=["targets"])
api_router.include_router(posts_router, prefix="/posts", tags=["posts"])
api_router.include_router(webhooks_router, prefix="/webhooks", tags=["webhooks"])
