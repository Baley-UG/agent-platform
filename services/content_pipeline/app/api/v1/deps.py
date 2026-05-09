"""FastAPI dependencies — auth and DB session."""

from __future__ import annotations

import secrets
import uuid
from typing import Iterator

from fastapi import Depends, Header, HTTPException, Path, status
from sqlmodel import Session

from app.core.config import settings
from app.models.projects import Project
from app.services.database import session_scope


def require_api_key(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> None:
    """Single static API key for the whole service (mirrors ig_scraper)."""
    expected = settings.CP_API_KEY
    if not x_api_key or not secrets.compare_digest(x_api_key, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid or missing X-API-Key")


def get_session() -> Iterator[Session]:
    """Yield a transactional DB session for request handlers."""
    with session_scope() as session:
        yield session


def get_project(
    project_id: uuid.UUID = Path(..., description="Project UUID"),
    session: Session = Depends(get_session),
) -> Project:
    """Resolve `{project_id}` path param to a Project row, 404 if missing."""
    project = session.get(Project, project_id)
    if project is None or project.status == "archived":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="project not found")
    return project
