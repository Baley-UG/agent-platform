"""Aggregate router for /api/v1/..."""

from fastapi import APIRouter

from app.api.v1 import assets, brand_kits, model_routes, music, projects, social_accounts, templates

api_router = APIRouter()

api_router.include_router(projects.router)
api_router.include_router(brand_kits.router)
api_router.include_router(social_accounts.router)
api_router.include_router(templates.router)
api_router.include_router(music.router)
api_router.include_router(assets.router)
api_router.include_router(model_routes.project_router)
api_router.include_router(model_routes.global_router)
