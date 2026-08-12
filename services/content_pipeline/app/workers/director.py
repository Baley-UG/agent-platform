"""RQ task — run the director LLM against a scenario.

Entry point: `app.workers.director.run(scenario_id)`.

Triggered by `POST /scenarios/{id}/run-director`. Materializes
`scene_renders` for the scenario's target aspect groups, runs the
director, stamps `resolved_asset_id` + `match_reason` per cell, and
records cost in `generation_calls`. Fail-open: a director failure
leaves the scenario in `pending_review` (or wherever it was) — the
admin can fall through to plain image_gen.
"""

from __future__ import annotations

import asyncio
import uuid

from app.core.logging import logger
from app.models.content_references import ContentReference
from app.models.projects import Project
from app.models.scenarios import Scenario
from app.services import director as director_svc
from app.services import generation_calls as calls_svc
from app.services import scene_renders as renders_svc
from app.services.database import session_scope


def run(scenario_id: str) -> dict:
    scenario_uuid = uuid.UUID(scenario_id)

    with session_scope() as session:
        scenario = session.get(Scenario, scenario_uuid)
        if scenario is None:
            logger.warning("director_scenario_missing", scenario_id=scenario_id)
            return {"ok": False, "error": "scenario not found"}
        project = session.get(Project, scenario.project_id)
        if project is None:
            return {"ok": False, "error": "project not found"}
        reference = session.get(ContentReference, scenario.reference_id)
        if reference is None:
            return {"ok": False, "error": "reference not found"}

        # Ensure scene_renders exist BEFORE the director runs — director
        # writes resolved_asset_id onto them. start-images uses the same
        # function so this is idempotent.
        renders_svc.materialize_for_scenario(session, scenario)

        try:
            result = asyncio.run(
                director_svc.run_director(
                    session=session,
                    project=project,
                    scenario=scenario,
                    reference=reference,
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "director_unhandled",
                scenario_id=scenario_id,
                error=str(exc),
            )
            return {"ok": False, "error": str(exc)}

        updates = director_svc.apply_director_result(
            session=session, scenario=scenario, result=result
        )

        if result.cost_usd is not None:
            calls_svc.record(
                session,
                project_id=project.id,
                scenario_id=scenario.id,
                variant_id=None,
                task_key="director",
                provider="openrouter",
                model_id="(auto)",
                cost_usd=result.cost_usd,
                latency_ms=result.latency_ms or 0,
                status_="success" if updates > 0 else "no_assignments",
            )

        return {
            "ok": True,
            "scenario_id": scenario_id,
            "assignments": [
                {
                    "scene_idx": a.scene_idx,
                    "resolved_asset_id": str(a.resolved_asset_id)
                    if a.resolved_asset_id
                    else None,
                    "match_reason": a.match_reason,
                    "confidence": a.confidence,
                }
                for a in result.assignments
            ],
            "gaps": result.gaps,
            "cells_stamped": updates,
            "cost_usd": result.cost_usd,
        }
