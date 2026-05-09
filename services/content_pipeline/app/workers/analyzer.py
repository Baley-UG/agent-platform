"""Analyzer RQ task.

Entry point: `app.workers.analyzer.run(scenario_id)` — dispatched from the
API when an admin creates or regenerates a scenario.

Resolves the LLM route → calls the provider → records the `generation_calls`
row → moves the scenario to `pending_review` (or `failed`).
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Optional

from app.core.logging import logger
from app.models.content_references import ContentReference
from app.models.scenarios import Scenario
from app.services import generation_calls as calls_svc
from app.services import model_router
from app.services import scenarios as scenarios_svc
from app.services.analyzer import analyze_reference
from app.services.database import session_scope
from app.services.providers.llm.base import LLMProvider
from app.services.providers.llm.openrouter import OpenRouterProvider


def _build_provider(provider_name: str) -> LLMProvider:
    """Pick the concrete LLM provider matching a route's `provider` field.

    Add new providers here (anthropic_direct, openai_direct, ollama_local…)
    as they're implemented.
    """
    if provider_name == "openrouter":
        return OpenRouterProvider()
    raise NotImplementedError(f"LLM provider not yet implemented: {provider_name}")


def run(scenario_id: str, brand_style_suffix: Optional[str] = None) -> dict:
    """RQ entry point. Returns a dict summary so the job result page is useful."""
    scenario_uuid = uuid.UUID(scenario_id)

    with session_scope() as session:
        scenario = session.get(Scenario, scenario_uuid)
        if scenario is None:
            logger.warning("analyzer_scenario_missing", scenario_id=scenario_id)
            return {"ok": False, "error": "scenario not found"}

        try:
            if scenario.status == "draft":
                scenarios_svc.transition(scenario, "analyzing")
                session.add(scenario)
                session.flush()
        except scenarios_svc.InvalidStateTransition as exc:
            logger.warning("analyzer_bad_state", scenario_id=scenario_id, status=scenario.status, error=str(exc))
            return {"ok": False, "error": str(exc)}

        if scenario.reference_id is None:
            scenarios_svc.mark_failed(session, scenario, "scenario has no reference_id")
            return {"ok": False, "error": "no reference"}

        reference = session.get(ContentReference, scenario.reference_id)
        if reference is None:
            scenarios_svc.mark_failed(session, scenario, "reference not found")
            return {"ok": False, "error": "reference missing"}

        try:
            route = model_router.resolve(session, "scenario_analysis", project_id=scenario.project_id)
        except model_router.NoRouteError as exc:
            scenarios_svc.mark_failed(session, scenario, f"no LLM route: {exc}")
            return {"ok": False, "error": str(exc)}

        provider = _build_provider(route.provider)

        try:
            scenario_json, response = asyncio.run(
                analyze_reference(
                    reference=reference,
                    route=route,
                    provider=provider,
                    brand_style_suffix=brand_style_suffix,
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("analyzer_call_failed", scenario_id=scenario_id, error=str(exc))
            scenarios_svc.mark_failed(session, scenario, str(exc))
            calls_svc.record(
                session,
                project_id=scenario.project_id,
                scenario_id=scenario.id,
                task_key="scenario_analysis",
                provider=route.provider,
                model_id=route.model_id,
                status_="failed",
                error=str(exc)[:1000],
            )
            return {"ok": False, "error": str(exc)}

        calls_svc.record(
            session,
            project_id=scenario.project_id,
            scenario_id=scenario.id,
            task_key="scenario_analysis",
            provider=route.provider,
            model_id=route.model_id,
            request_id=response.request_id,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            cached_tokens=response.cached_tokens,
            cost_usd=response.cost_usd,
            latency_ms=response.latency_ms,
            status_="success",
        )

        scenarios_svc.mark_pending_review(session, scenario, scenario_json)
        return {
            "ok": True,
            "scenario_id": str(scenario.id),
            "model": route.model_id,
            "input_tokens": response.input_tokens,
            "output_tokens": response.output_tokens,
            "cost_usd": response.cost_usd,
        }
