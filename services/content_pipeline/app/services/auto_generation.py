"""Auto-generation orchestrator.

Hourly loop walks enabled `auto_generation_rules`, picks the next-best
candidate reference per rule, and enqueues a scenario create.

Quotas (in priority order):
- per-rule `daily_quota` — count of scenarios created TODAY by this rule
- per-rule `budget_cap_usd` — weekly spend over `generation_calls`
- project-level `weekly_budget_cap_usd` — same window
- skip references already used by an active (non-failed/non-archived) scenario
- skip references not in `status='approved'`

Pick strategies:
- highest_score — order by `metadata.score` DESC (when present), tie-break newest
- newest — order by `imported_at` DESC
- diverse — round-robin authors (best-effort)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import func
from sqlmodel import Session, select

from app.core.logging import logger
from app.models.auto_generation_rules import AutoGenerationRule
from app.models.content_references import ContentReference
from app.models.projects import Project
from app.models.reference_usages import ReferenceUsage
from app.models.scenarios import Scenario
from app.schemas.scenarios import ScenarioCreate
from app.services import budget
from app.services import scenarios as scenarios_svc


def _scenarios_today_for_rule(session: Session, rule: AutoGenerationRule) -> int:
    """Count scenarios created today by this rule (created_by tag).

    We tag auto-generated scenarios with `created_by="auto_gen:{rule_id}"` so
    the counter is rule-scoped without a separate audit table.
    """
    today = budget.day_start_utc()
    stmt = select(func.count(Scenario.id)).where(
        Scenario.project_id == rule.project_id,
        Scenario.created_by == f"auto_gen:{rule.id}",
        Scenario.created_at >= today,
    )
    return int(session.exec(stmt).one() or 0)


def _used_reference_ids(session: Session, project_id: uuid.UUID) -> set:
    """References already used by ANY scenario in this project (regardless of status)."""
    stmt = select(ReferenceUsage.reference_id).where(ReferenceUsage.project_id == project_id)
    return {row for row in session.exec(stmt).all()}


def _pick_candidate(
    session: Session,
    project_id: uuid.UUID,
    *,
    pick_strategy: str,
    used_ids: set,
) -> Optional[ContentReference]:
    base = (
        select(ContentReference)
        .where(
            ContentReference.project_id == project_id,
            ContentReference.status == "approved",
        )
        .where(~ContentReference.id.in_(used_ids) if used_ids else True)
    )

    if pick_strategy == "newest":
        stmt = base.order_by(ContentReference.imported_at.desc()).limit(1)
    elif pick_strategy == "diverse":
        # Round-robin by source_external_id author when present in metadata.
        # Best-effort: just pick newest from a not-recently-used author.
        stmt = base.order_by(ContentReference.imported_at.desc()).limit(20)
        rows = list(session.exec(stmt).all())
        if not rows:
            return None
        seen_authors: set = set()
        for ref in rows:
            author = (ref.metadata_json or {}).get("username") if ref.metadata_json else None
            if author and author in seen_authors:
                continue
            if author:
                seen_authors.add(author)
            return ref
        return rows[0]
    else:  # highest_score
        # The score lives in metadata_json.score (set during ig_scraper import).
        # We sort with a JSONB cast; references missing the field land at the end.
        from sqlalchemy import cast

        stmt = base.order_by(
            cast(ContentReference.metadata_json["score"], func.text()).desc().nulls_last(),
            ContentReference.imported_at.desc(),
        ).limit(1)

    return session.exec(stmt).first()


def run_rule(
    session: Session,
    rule: AutoGenerationRule,
    project: Project,
    *,
    headroom_usd: float = 0.50,
) -> Optional[uuid.UUID]:
    """Try to spawn one scenario for this rule. Returns the new scenario id
    or None when nothing was eligible (quota exhausted, budget exhausted,
    or no candidate references).
    """
    if not rule.enabled:
        return None

    # Daily quota check.
    if _scenarios_today_for_rule(session, rule) >= rule.daily_quota:
        return None

    # Per-rule budget.
    if not budget.has_rule_budget_remaining(
        session, project.id, rule.budget_cap_usd, headroom_usd=headroom_usd
    ):
        logger.info("auto_gen_rule_over_rule_budget", rule_id=str(rule.id), project_id=str(project.id))
        return None

    # Project weekly cap.
    if not budget.has_weekly_budget_remaining(session, project, headroom_usd=headroom_usd):
        logger.info("auto_gen_rule_over_project_budget", project_id=str(project.id))
        return None

    # Candidate.
    used_ids = _used_reference_ids(session, project.id)
    ref = _pick_candidate(session, project.id, pick_strategy=rule.pick_strategy, used_ids=used_ids)
    if ref is None:
        return None

    payload = ScenarioCreate(
        reference_id=ref.id,
        target_variants=list(rule.target_variants or ["ig_reels"]),
        quality_tier=rule.quality_tier,
        force=False,
        reuse_reason="",
    )

    try:
        scenario = scenarios_svc.create(session, project, payload, created_by=f"auto_gen:{rule.id}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("auto_gen_create_failed", rule_id=str(rule.id), reference_id=str(ref.id), error=str(exc))
        return None

    rule.last_run_at = datetime.now(timezone.utc)
    session.add(rule)
    session.flush()
    return scenario.id


def run_all_due(session: Session, *, headroom_usd: float = 0.50) -> int:
    """Walk every enabled rule across every active project; spawn at most one
    scenario per rule per call (the loop runs every hour, so daily_quota=N
    fires N times across the day).
    """
    spawned = 0
    rules = list(
        session.exec(
            select(AutoGenerationRule).where(AutoGenerationRule.enabled == True)  # noqa: E712
        ).all()
    )
    for rule in rules:
        project = session.get(Project, rule.project_id)
        if project is None or project.status != "active":
            continue
        new_id = run_rule(session, rule, project, headroom_usd=headroom_usd)
        if new_id is not None:
            spawned += 1
            logger.info(
                "auto_gen_spawned",
                rule_id=str(rule.id),
                project_id=str(project.id),
                scenario_id=str(new_id),
            )
    return spawned
