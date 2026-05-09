"""Scheduler entry point.

Runs five concurrent tasks:
- heartbeat (every CP_SCHEDULER_TICK_SECONDS) — cheap liveness log.
- publisher poller (every minute) — find `plan_slots` whose
  `scheduled_at <= now()` and `status='ready'`, enqueue a publish job.
- plan filler (hourly) — re-run fill_strategy on draft plans (e.g. after
  new stock arrived) so the auto_suggest list grows.
- weekly auto-generator (Sunday 18:00 UTC) — for every project, ensure
  next week's weekly_plan exists.
- stale-job alerter (hourly) — log a warning for any scenario stuck in
  a `generating_*` status for >2 hours.

Idempotency: in-memory date/hour markers prevent double-runs across
ticks within the same window. Restarts re-arm naturally.
"""

from __future__ import annotations

import asyncio
import signal
from datetime import date, datetime, time, timedelta, timezone
from typing import Set

from redis import Redis
from sqlmodel import select

from app.core.config import settings
from app.core.logging import logger
from app.models.projects import Project
from app.models.scenarios import Scenario
from app.models.weekly_plans import WeeklyPlan
from app.services import queue
from app.services import weekly_plans as plans_svc
from app.services.database import session_scope


_shutdown = asyncio.Event()


# ----- helper crons -----


async def _heartbeat_loop() -> None:
    interval = settings.CP_SCHEDULER_TICK_SECONDS
    while not _shutdown.is_set():
        logger.info("content_pipeline_scheduler_tick", interval_s=interval)
        try:
            await asyncio.wait_for(_shutdown.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue


async def _publisher_poller_loop() -> None:
    """Every minute, enqueue publish jobs for due slots."""
    while not _shutdown.is_set():
        try:
            with session_scope() as session:
                due = plans_svc.due_slots(session)
                for slot in due:
                    try:
                        queue.enqueue("publish", "app.workers.publish.run", str(slot.id))
                        slot.status = "scheduled"
                        session.add(slot)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("publisher_enqueue_failed", slot_id=str(slot.id), error=str(exc))
                if due:
                    session.flush()
                    logger.info("publisher_poller_enqueued", count=len(due))
        except Exception as exc:  # noqa: BLE001
            logger.warning("publisher_poller_error", error=str(exc))

        try:
            await asyncio.wait_for(_shutdown.wait(), timeout=60)
        except asyncio.TimeoutError:
            continue


async def _plan_filler_loop() -> None:
    """Hourly — refill empty slots in active weekly_plans (new stock may have arrived)."""
    while not _shutdown.is_set():
        try:
            with session_scope() as session:
                stmt = select(WeeklyPlan).where(WeeklyPlan.status.in_(("draft", "approved")))
                plans = list(session.exec(stmt).all())
                refilled = 0
                for plan in plans:
                    refilled += plans_svc.fill_empty_slots(session, plan)
                if refilled:
                    logger.info("plan_filler_refilled", slots=refilled)
        except Exception as exc:  # noqa: BLE001
            logger.warning("plan_filler_error", error=str(exc))

        try:
            await asyncio.wait_for(_shutdown.wait(), timeout=3600)
        except asyncio.TimeoutError:
            continue


_AUTOGEN_RUN_DATES: Set[date] = set()
_STALE_RUN_HOURS: Set[int] = set()


async def _weekly_autogen_loop() -> None:
    """Every Sunday 18:00 UTC, ensure next week's weekly_plan exists for every project."""
    while not _shutdown.is_set():
        try:
            now = datetime.now(timezone.utc)
            today = now.date()
            target = datetime.combine(today, time(hour=18, minute=0), tzinfo=timezone.utc)
            if (
                now.weekday() == 6  # Sunday
                and now >= target
                and today not in _AUTOGEN_RUN_DATES
            ):
                _autogen_next_week(now)
                _AUTOGEN_RUN_DATES.add(today)
        except Exception as exc:  # noqa: BLE001
            logger.warning("weekly_autogen_error", error=str(exc))

        try:
            await asyncio.wait_for(_shutdown.wait(), timeout=300)
        except asyncio.TimeoutError:
            continue


def _autogen_next_week(now: datetime) -> None:
    next_monday = (now + timedelta(days=(7 - now.weekday()))).date()
    with session_scope() as session:
        projects = session.exec(select(Project).where(Project.status == "active")).all()
        for project in projects:
            try:
                plans_svc.generate(session, project, next_monday, fill=True, generated_by="scheduler")
                logger.info("weekly_autogen_done", project=str(project.id), week_start=str(next_monday))
            except Exception as exc:  # noqa: BLE001
                logger.warning("weekly_autogen_project_failed", project=str(project.id), error=str(exc))


async def _stale_alerter_loop() -> None:
    """Hourly — log scenarios stuck in `generating_*` / `composing` / `analyzing` for >2h."""
    while not _shutdown.is_set():
        try:
            now = datetime.now(timezone.utc)
            current_hour = now.hour
            if current_hour not in _STALE_RUN_HOURS:
                _STALE_RUN_HOURS.add(current_hour)
                if len(_STALE_RUN_HOURS) >= 24:
                    _STALE_RUN_HOURS.clear()
                    _STALE_RUN_HOURS.add(current_hour)
                with session_scope() as session:
                    threshold = now - timedelta(hours=2)
                    stmt = select(Scenario).where(
                        Scenario.status.in_(
                            ("generating_images", "generating_videos", "generating_audio", "composing", "analyzing")
                        ),
                        Scenario.updated_at < threshold,
                    )
                    stale = list(session.exec(stmt).all())
                    for s in stale:
                        logger.warning(
                            "scenario_stale",
                            scenario_id=str(s.id),
                            status=s.status,
                            updated_at=str(s.updated_at),
                        )
        except Exception as exc:  # noqa: BLE001
            logger.warning("stale_alerter_error", error=str(exc))

        try:
            await asyncio.wait_for(_shutdown.wait(), timeout=3600)
        except asyncio.TimeoutError:
            continue


# ----- top-level -----


async def run() -> None:
    redis_conn = Redis.from_url(settings.redis_url)
    try:
        redis_conn.ping()
    except Exception as exc:  # noqa: BLE001
        logger.warning("scheduler_redis_unreachable", error=str(exc))

    logger.info("content_pipeline_scheduler_starting", redis_db=settings.REDIS_DB)
    await asyncio.gather(
        _heartbeat_loop(),
        _publisher_poller_loop(),
        _plan_filler_loop(),
        _weekly_autogen_loop(),
        _stale_alerter_loop(),
    )
    logger.info("content_pipeline_scheduler_stopped")


def _install_signal_handlers(loop: asyncio.AbstractEventLoop) -> None:
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown.set)


def main() -> None:
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        _install_signal_handlers(loop)
        loop.run_until_complete(run())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
