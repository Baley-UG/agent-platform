"""reference_intake_rules CRUD + matcher.

The matcher walks enabled rules in `priority` ASC order and returns the
first one whose `conditions` dict matches the candidate. CP-M2 ships only
the data + matcher; the rule-driven scraper subscriber lands in CP-M2.5.
"""

from __future__ import annotations

import uuid
from typing import List, Optional

from fastapi import HTTPException, status
from sqlmodel import Session, select

from app.models.reference_intake_rules import ReferenceIntakeRule
from app.schemas.intake_rules import IntakeRuleCreate, IntakeRuleUpdate


def create(session: Session, project_id: uuid.UUID, payload: IntakeRuleCreate) -> ReferenceIntakeRule:
    rule = ReferenceIntakeRule(project_id=project_id, **payload.model_dump())
    session.add(rule)
    session.flush()
    session.refresh(rule)
    return rule


def list_(session: Session, project_id: uuid.UUID) -> List[ReferenceIntakeRule]:
    stmt = (
        select(ReferenceIntakeRule)
        .where(ReferenceIntakeRule.project_id == project_id)
        .order_by(ReferenceIntakeRule.priority, ReferenceIntakeRule.created_at)
    )
    return list(session.exec(stmt).all())


def get(session: Session, project_id: uuid.UUID, rule_id: uuid.UUID) -> ReferenceIntakeRule:
    row = session.get(ReferenceIntakeRule, rule_id)
    if row is None or row.project_id != project_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="intake_rule not found")
    return row


def update(
    session: Session, project_id: uuid.UUID, rule_id: uuid.UUID, payload: IntakeRuleUpdate
) -> ReferenceIntakeRule:
    row = get(session, project_id, rule_id)
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(row, key, value)
    session.add(row)
    session.flush()
    session.refresh(row)
    return row


def delete(session: Session, project_id: uuid.UUID, rule_id: uuid.UUID) -> None:
    row = get(session, project_id, rule_id)
    session.delete(row)
    session.flush()


# ----- Matching engine -----

# Recognized condition keys. Unknown keys are ignored (forward-compatible).
_NUMERIC_GTE = {
    "min_score": "score",
    "min_engagement_rate": "engagement_rate",
    "min_likes": "like_count",
    "min_play_count": "play_count",
    "posted_within_days": None,  # special-cased below
    "min_duration_sec": "duration_sec",
}
_NUMERIC_LTE = {
    "max_duration_sec": "duration_sec",
}


def matches(rule: ReferenceIntakeRule, candidate: dict) -> bool:
    """Check whether a candidate dict satisfies a rule's `conditions`.

    `candidate` keys (any subset, missing → fail closed):
      - score, engagement_rate, like_count, play_count, duration_sec
      - posted_at (datetime or ISO str), media_type, language
      - has_caption, hashtags (list[str]), tracked_target (str | None)
    """
    conds = rule.conditions or {}

    for key, candidate_key in _NUMERIC_GTE.items():
        if key not in conds:
            continue
        if key == "posted_within_days":
            from datetime import datetime, timezone

            posted = candidate.get("posted_at")
            if isinstance(posted, str):
                try:
                    posted = datetime.fromisoformat(posted)
                except ValueError:
                    return False
            if not isinstance(posted, datetime):
                return False
            if posted.tzinfo is None:
                posted = posted.replace(tzinfo=timezone.utc)
            delta = datetime.now(timezone.utc) - posted
            if delta.days > conds[key]:
                return False
            continue
        value = candidate.get(candidate_key)
        if value is None or value < conds[key]:
            return False

    for key, candidate_key in _NUMERIC_LTE.items():
        if key not in conds:
            continue
        value = candidate.get(candidate_key)
        if value is None or value > conds[key]:
            return False

    if "media_types" in conds:
        if candidate.get("media_type") not in conds["media_types"]:
            return False

    if "language" in conds:
        if candidate.get("language") not in conds["language"]:
            return False

    if conds.get("must_have_caption"):
        if not candidate.get("has_caption"):
            return False

    if "from_tracked_targets" in conds:
        if candidate.get("tracked_target") not in conds["from_tracked_targets"]:
            return False

    return True


def first_match(session: Session, project_id: uuid.UUID, candidate: dict) -> Optional[ReferenceIntakeRule]:
    """Return the first enabled rule that matches a candidate (lowest priority wins)."""
    for rule in list_(session, project_id):
        if not rule.enabled:
            continue
        if matches(rule, candidate):
            return rule
    return None
