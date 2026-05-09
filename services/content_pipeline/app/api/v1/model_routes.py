"""Model route CRUD endpoints — both project-scoped and global.

The router is mounted twice via `api.py` so admins can manage both:
- `/projects/{pid}/model-routes/...` for per-project overrides
- `/global/model-routes/...` for cross-project defaults
"""

from __future__ import annotations

import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, status
from sqlmodel import Session

from app.api.v1.deps import get_project, get_session, require_api_key
from app.models.projects import Project
from app.schemas.model_routes import ModelRouteCreate, ModelRouteRead, ModelRouteUpdate
from app.services import model_router as svc

# --- Project-scoped router ---
project_router = APIRouter(
    prefix="/projects/{project_id}/model-routes",
    tags=["model-routes"],
    dependencies=[Depends(require_api_key)],
)

# --- Global (admin) router ---
global_router = APIRouter(
    prefix="/global/model-routes",
    tags=["model-routes"],
    dependencies=[Depends(require_api_key)],
)


def _list(session: Session, project_id: Optional[uuid.UUID]) -> List[ModelRouteRead]:
    return [ModelRouteRead.model_validate(r) for r in svc.list_for_project(session, project_id)]


@project_router.get("", response_model=List[ModelRouteRead])
def list_for_project(
    project: Project = Depends(get_project), session: Session = Depends(get_session)
) -> List[ModelRouteRead]:
    return _list(session, project.id)


@project_router.post("", response_model=ModelRouteRead, status_code=status.HTTP_201_CREATED)
def create_for_project(
    payload: ModelRouteCreate,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> ModelRouteRead:
    return ModelRouteRead.model_validate(svc.create(session, project.id, payload))


@project_router.patch("/{route_id}", response_model=ModelRouteRead)
def update_route(
    route_id: uuid.UUID,
    payload: ModelRouteUpdate,
    session: Session = Depends(get_session),
) -> ModelRouteRead:
    return ModelRouteRead.model_validate(svc.update(session, route_id, payload))


@project_router.delete("/{route_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_route(route_id: uuid.UUID, session: Session = Depends(get_session)) -> None:
    svc.delete(session, route_id)


# --- Global routes ---
@global_router.get("", response_model=List[ModelRouteRead])
def list_global(session: Session = Depends(get_session)) -> List[ModelRouteRead]:
    return _list(session, None)


@global_router.post("", response_model=ModelRouteRead, status_code=status.HTTP_201_CREATED)
def create_global(payload: ModelRouteCreate, session: Session = Depends(get_session)) -> ModelRouteRead:
    return ModelRouteRead.model_validate(svc.create(session, None, payload))


@global_router.patch("/{route_id}", response_model=ModelRouteRead)
def update_global(
    route_id: uuid.UUID, payload: ModelRouteUpdate, session: Session = Depends(get_session)
) -> ModelRouteRead:
    return ModelRouteRead.model_validate(svc.update(session, route_id, payload))


@global_router.delete("/{route_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_global(route_id: uuid.UUID, session: Session = Depends(get_session)) -> None:
    svc.delete(session, route_id)
