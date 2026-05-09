"""posting_strategy CRUD — one row per project, lazy-create on first read."""

from __future__ import annotations

import uuid
from typing import Optional

from sqlmodel import Session, select

from app.models.posting_strategy import PostingStrategy


def get_or_create(session: Session, project_id: uuid.UUID) -> PostingStrategy:
    row = session.exec(select(PostingStrategy).where(PostingStrategy.project_id == project_id)).first()
    if row is not None:
        return row
    row = PostingStrategy(project_id=project_id)
    session.add(row)
    session.flush()
    session.refresh(row)
    return row


def update(session: Session, project_id: uuid.UUID, patch: dict) -> PostingStrategy:
    row = get_or_create(session, project_id)
    allowed = {
        "timezone",
        "weekly_quota",
        "preferred_slots",
        "min_gap_minutes",
        "blackout",
        "fill_strategy",
        "auto_generate_if_empty",
        "approval_required_before_publish",
        "weekly_budget_cap_usd",
    }
    for key, value in patch.items():
        if key in allowed and value is not None:
            setattr(row, key, value)
    session.add(row)
    session.flush()
    session.refresh(row)
    return row
