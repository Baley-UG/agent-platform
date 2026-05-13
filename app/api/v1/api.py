"""API v1 router configuration.

This module sets up the main API router and includes all sub-routers for different
endpoints like authentication and chatbot functionality.
"""

from fastapi import APIRouter

from app.api.v1.admin_auth import router as admin_auth_router
from app.api.v1.admin_gateway import router as admin_gateway_router
from app.api.v1.admin_projects import router as admin_projects_router
from app.api.v1.admin_users import membership_router as admin_membership_router
from app.api.v1.admin_users import users_router as admin_users_router
from app.api.v1.auth import router as auth_router
from app.api.v1.chatbot import router as chatbot_router
from app.api.v1.marketing import router as marketing_router
from app.api.v1.slack import router as slack_router
from app.core.logging import logger

api_router = APIRouter()

# Existing chatbot/main app routers
api_router.include_router(auth_router, prefix="/auth", tags=["auth"])
api_router.include_router(chatbot_router, prefix="/chatbot", tags=["chatbot"])
api_router.include_router(marketing_router, prefix="/marketing", tags=["marketing"])
api_router.include_router(slack_router, prefix="/slack", tags=["slack"])

# CP-M9 admin panel routers (auth + users + memberships + projects)
api_router.include_router(admin_auth_router)
api_router.include_router(admin_users_router)
api_router.include_router(admin_membership_router)
# Project entity CRUD lives in main app (table is public.projects);
# downstream sub-resources (brand-kits, scenarios, plan-slots, etc.)
# remain under /cp/projects/{pid}/... via the gateway.
api_router.include_router(admin_projects_router)

# Hybrid gateway — proxies to content_pipeline + ig_scraper. Registered
# LAST so explicit endpoints above take precedence over the catch-all.
api_router.include_router(admin_gateway_router)


@api_router.get("/health")
async def health_check():
    """Health check endpoint.

    Returns:
        dict: Health status information.
    """
    logger.info("health_check_called")
    return {"status": "healthy", "version": "1.0.0"}
