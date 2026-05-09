"""content_references endpoints — manual upload, import-from-scraper, list, archive, usage-check."""

from __future__ import annotations

import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, Query, status
from sqlmodel import Session

from app.api.v1.deps import get_project, get_session, require_api_key
from app.models.projects import Project
from app.schemas.references import (
    ReferenceImportFromScraper,
    ReferenceManualUpload,
    ReferenceRead,
    ReferenceUpdate,
    UsageCheck,
)
from app.services import references as svc

router = APIRouter(
    prefix="/projects/{project_id}/references",
    tags=["references"],
    dependencies=[Depends(require_api_key)],
)


@router.post("/upload", response_model=ReferenceRead, status_code=status.HTTP_201_CREATED)
def upload(
    payload: ReferenceManualUpload,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> ReferenceRead:
    """Register a reference whose media bytes have already been PUT to S3.

    Typical flow: admin calls `/assets/upload-url`, PUTs the file, then calls
    this endpoint with the returned `s3_key`.
    """
    return ReferenceRead.model_validate(svc.manual_upload(session, project.id, payload))


@router.post("/import-from-scraper", response_model=ReferenceRead, status_code=status.HTTP_201_CREATED)
def import_from_scraper(
    payload: ReferenceImportFromScraper,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> ReferenceRead:
    """Pull an `ig_scraper.ig_posts` row into the reference pool by media pk."""
    return ReferenceRead.model_validate(svc.import_from_scraper(session, project.id, payload))


@router.get("", response_model=List[ReferenceRead])
def list_(
    status_: Optional[str] = Query(default=None, alias="status"),
    source_provider: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> List[ReferenceRead]:
    return [
        ReferenceRead.model_validate(r)
        for r in svc.list_(session, project.id, status_=status_, source_provider=source_provider, limit=limit, offset=offset)
    ]


@router.get("/{reference_id}", response_model=ReferenceRead)
def get(
    reference_id: uuid.UUID,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> ReferenceRead:
    return ReferenceRead.model_validate(svc.get(session, project.id, reference_id))


@router.patch("/{reference_id}", response_model=ReferenceRead)
def update(
    reference_id: uuid.UUID,
    payload: ReferenceUpdate,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> ReferenceRead:
    return ReferenceRead.model_validate(svc.update(session, project.id, reference_id, payload))


@router.post("/{reference_id}/archive", response_model=ReferenceRead)
def archive(
    reference_id: uuid.UUID,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> ReferenceRead:
    return ReferenceRead.model_validate(svc.archive(session, project.id, reference_id))


@router.get("/{reference_id}/usage-check", response_model=UsageCheck)
def usage_check(
    reference_id: uuid.UUID,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> UsageCheck:
    return svc.usage_check(session, project, reference_id)


@router.get("/{reference_id}/dedup-check")
def dedup_check(
    reference_id: uuid.UUID,
    max_distance: int = 6,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> dict:
    """Find near-duplicates of THIS reference by perceptual hash.

    Returns up to 10 references in the same project within Hamming
    distance `max_distance` of this reference's `content_hash`. Empty
    list when this row has no hash yet (CP-M8.5 will populate hashes
    at import time).
    """
    from app.services import dedup as dedup_svc

    ref = svc.get(session, project.id, reference_id)
    if not ref.content_hash:
        return {"reference_id": str(ref.id), "has_hash": False, "matches": []}
    matches = dedup_svc.find_near_duplicates(
        session, project.id, bytes(ref.content_hash), max_distance=max_distance, exclude_id=ref.id
    )
    return {
        "reference_id": str(ref.id),
        "has_hash": True,
        "max_distance": max_distance,
        "matches": [
            {
                "id": str(m.id),
                "distance": dist,
                "source_provider": m.source_provider,
                "imported_at": m.imported_at.isoformat() if m.imported_at else None,
                "caption": (m.caption or "")[:160],
            }
            for m, dist in matches
        ],
    }


@router.post("/{reference_id}/curate")
async def curate(
    reference_id: uuid.UUID,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> dict:
    """Run the AI curator over this reference.

    Calls the LLM (via the project's `scenario_analysis` route, since the
    curator is just an LLM-backed scoring task), writes
    `curator_score` + `curator_reason` on the row, returns the new values.
    Fail-open: returns `score=null` when no LLM route is configured.
    """
    from app.services import curator as curator_svc

    ref = svc.get(session, project.id, reference_id)
    score, reason = await curator_svc.curate(session, project, ref)
    return {
        "reference_id": str(ref.id),
        "curator_score": float(score) if score is not None else None,
        "curator_reason": reason,
    }
