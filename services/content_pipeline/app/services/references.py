"""content_references CRUD + ingestion helpers."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.models.content_references import ContentReference
from app.models.projects import Project
from app.models.reference_usages import ReferenceUsage
from app.schemas.references import ReferenceImportFromScraper, ReferenceManualUpload, ReferenceUpdate, UsageCheck
from app.services import scraper_bridge


def _commit_reference(session: Session, ref: ContentReference) -> ContentReference:
    session.add(ref)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="reference already imported (project_id + source_provider + source_external_id)",
        ) from exc
    session.refresh(ref)
    return ref


def manual_upload(
    session: Session, project_id: uuid.UUID, payload: ReferenceManualUpload, created_by: Optional[str] = None
) -> ContentReference:
    ref = ContentReference(
        project_id=project_id,
        source_provider="manual_upload",
        source_external_id=None,
        source_url=payload.source_url,
        media_s3_key=payload.media_s3_key,
        poster_s3_key=payload.poster_s3_key,
        caption=payload.caption,
        transcript=payload.transcript,
        hashtags=payload.hashtags,
        metadata_json=payload.metadata,
        status="approved" if payload.auto_approve else "candidate",
        imported_by=created_by or "manual",
    )
    return _commit_reference(session, ref)


def import_from_scraper(
    session: Session, project_id: uuid.UUID, payload: ReferenceImportFromScraper, created_by: Optional[str] = None
) -> ContentReference:
    """Pull an `ig_scraper.ig_posts` row across the cross-DB read engine."""
    raw = scraper_bridge.fetch_ig_post(payload.ig_post_id)
    if raw is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"ig_post {payload.ig_post_id} not found in ig_scraper.ig_posts",
        )

    media_urls = raw.get("media_urls") or []
    metadata = {
        "shortcode": raw.get("shortcode"),
        "username": raw.get("username"),
        "taken_at": raw["taken_at"].isoformat() if raw.get("taken_at") else None,
        "like_count": raw.get("like_count"),
        "comment_count": raw.get("comment_count"),
        "play_count": raw.get("play_count"),
        "view_count": raw.get("view_count"),
        "media_type": raw.get("media_type"),
        "product_type": raw.get("product_type"),
        "score": float(raw["score"]) if raw.get("score") is not None else None,
        "ig_media_urls": media_urls,
        "ig_thumbnail_url": raw.get("thumbnail_url"),
    }
    source_url = (
        f"https://www.instagram.com/reel/{raw['shortcode']}/" if raw.get("shortcode") else None
    )

    ref = ContentReference(
        project_id=project_id,
        source_provider="instagram",
        source_external_id=str(raw["source_external_id"]),
        source_url=source_url,
        # We don't auto-download the media here — caller can fetch lazily
        # when the analyzer needs it. CP-M2.5/CP-M3 may add a worker that
        # mirrors `media_urls[0]` into S3 on import.
        media_s3_key=None,
        poster_s3_key=None,
        caption=raw.get("caption"),
        transcript=None,
        hashtags=None,
        metadata_json=metadata,
        status="approved" if payload.auto_approve else "candidate",
        imported_by=created_by or "import-from-scraper",
    )
    return _commit_reference(session, ref)


def list_(
    session: Session,
    project_id: uuid.UUID,
    status_: Optional[str] = None,
    source_provider: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> List[ContentReference]:
    stmt = select(ContentReference).where(ContentReference.project_id == project_id)
    if status_:
        stmt = stmt.where(ContentReference.status == status_)
    if source_provider:
        stmt = stmt.where(ContentReference.source_provider == source_provider)
    stmt = stmt.order_by(ContentReference.imported_at.desc()).limit(limit).offset(offset)
    return list(session.exec(stmt).all())


def get(session: Session, project_id: uuid.UUID, reference_id: uuid.UUID) -> ContentReference:
    row = session.get(ContentReference, reference_id)
    if row is None or row.project_id != project_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="reference not found")
    return row


def update(
    session: Session, project_id: uuid.UUID, reference_id: uuid.UUID, payload: ReferenceUpdate
) -> ContentReference:
    row = get(session, project_id, reference_id)
    data = payload.model_dump(exclude_unset=True)
    if "metadata" in data:
        row.metadata_json = data.pop("metadata")
    for key, value in data.items():
        setattr(row, key, value)
    session.add(row)
    session.flush()
    session.refresh(row)
    return row


def archive(session: Session, project_id: uuid.UUID, reference_id: uuid.UUID) -> ContentReference:
    row = get(session, project_id, reference_id)
    row.status = "archived"
    session.add(row)
    session.flush()
    session.refresh(row)
    return row


def usage_check(session: Session, project: Project, reference_id: uuid.UUID) -> UsageCheck:
    """Return reuse history + the project's policy."""
    get(session, project.id, reference_id)  # ensure exists & belongs to project

    stmt = (
        select(ReferenceUsage)
        .where(ReferenceUsage.reference_id == reference_id)
        .order_by(ReferenceUsage.created_at.desc())
    )
    rows = list(session.exec(stmt).all())
    last_used_days_ago = None
    if rows:
        delta = datetime.now(timezone.utc) - rows[0].created_at
        last_used_days_ago = max(delta.days, 0)
    previous = [
        {
            "scenario_id": str(r.scenario_id),
            "status": r.status,
            "created_at": r.created_at.isoformat(),
            "reuse_reason": r.reuse_reason or None,
        }
        for r in rows
    ]
    return UsageCheck(
        reference_id=reference_id,
        previously_used=bool(rows),
        usage_count=len(rows),
        last_used_days_ago=last_used_days_ago,
        previous_scenarios=previous,
        project_reuse_policy=project.reuse_policy,
    )
