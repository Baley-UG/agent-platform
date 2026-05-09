"""Aggregate router for /api/v1/..."""

from fastapi import APIRouter

from app.api.v1 import (
    assets,
    brand_kits,
    cost,
    intake_rules,
    media_assets,
    model_routes,
    music,
    plans,
    posting_strategy,
    projects,
    publish,
    references,
    scenarios,
    social_accounts,
    templates,
)

api_router = APIRouter()

api_router.include_router(projects.router)
api_router.include_router(brand_kits.router)
api_router.include_router(social_accounts.router)
api_router.include_router(templates.router)
api_router.include_router(music.router)
api_router.include_router(assets.router)
api_router.include_router(media_assets.router)
api_router.include_router(references.router)
api_router.include_router(intake_rules.router)
api_router.include_router(scenarios.router)
api_router.include_router(cost.router)
api_router.include_router(posting_strategy.router)
api_router.include_router(plans.plan_router)
api_router.include_router(plans.slot_router)
api_router.include_router(plans.stock_router)
api_router.include_router(publish.router)
api_router.include_router(model_routes.project_router)
api_router.include_router(model_routes.global_router)
