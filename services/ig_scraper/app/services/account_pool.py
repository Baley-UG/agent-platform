"""Account pool — picks a healthy scraping account for a job.

The pool is the single chokepoint between "we have work to do" and "we
make a request to Instagram". Every gating rule (active hours, daily
quota, cooldown, role match, status, sticky proxy) lives here so the
worker loop stays simple.

Acquire/release are transactional. The caller owns the lifecycle:
    account = acquire(session, job)
    try:
        ...do work...
    finally:
        release(session, account, outcome)

The acquire side uses `SELECT ... FOR UPDATE SKIP LOCKED` so two
workers can never grab the same account row.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from random import randint
from typing import Iterable, Literal, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlmodel import Session

from app.core.config import settings
from app.core.logging import logger
from app.models.account import Account
from app.models.job import ScrapeJob
from app.models.proxy import Proxy

ReleaseOutcome = Literal["success", "soft_fail", "challenge", "rate_limited", "fatal"]


_QUOTA_BY_TIER = {
    "fresh": "IG_DAILY_QUOTA_FRESH",
    "mid": "IG_DAILY_QUOTA_MID",
    "warm": "IG_DAILY_QUOTA_WARM",
}


@dataclass
class AcquiredAccount:
    """Bundle of what the worker needs to actually run a job."""

    account: Account
    proxy: Optional[Proxy]


class NoAccountAvailable(Exception):
    """Raised when no account satisfies all the gating rules right now."""


# ---------------------------------------------------------------------
# Acquire
# ---------------------------------------------------------------------

# Pre-filter at the SQL level: status='active', not in cooldown, role
# matches. We rank by `last_used_at NULLS FIRST` so newly-onboarded
# accounts get exercised. RANDOM() tiebreaker keeps load even.
_CLAIM_SQL = text(
    """
    SELECT id FROM ig_accounts
    WHERE status = 'active'
      AND role = :required_role
      AND (cooldown_until IS NULL OR cooldown_until <= now())
    ORDER BY last_used_at NULLS FIRST, RANDOM()
    FOR UPDATE SKIP LOCKED
    """
)


def _required_role_for_job(job: ScrapeJob) -> str:
    """Canary jobs use canary accounts; everything else uses scrapers."""
    return "canary" if (job.params or {}).get("canary") else "scraper"


def _within_active_hours(account: Account, now: datetime) -> bool:
    """Honour the per-account active-hours window in its declared TZ.

    Active window is [start, end) on the account's local clock; weekday
    bitmap is Mon=1, Tue=2, ... Sun=64 (so 127 = all days).
    """
    try:
        local = now.astimezone(ZoneInfo(account.timezone or "UTC"))
    except Exception:  # noqa: BLE001
        local = now.astimezone(timezone.utc)
    weekday_bit = 1 << local.weekday()  # Mon=0 → bit 0 (=1)
    if not (account.weekday_pattern & weekday_bit):
        return False
    start = account.active_hours_start
    end = account.active_hours_end
    if start <= end:
        return start <= local.hour < end
    # Wrap-around (e.g. 22..6).
    return local.hour >= start or local.hour < end


def _quota_remaining(session: Session, account: Account) -> int:
    """Today's calls_made vs the tier-specific cap.

    Returns the remaining headroom — negative or zero means quota
    exceeded.
    """
    tier_attr = _QUOTA_BY_TIER.get(account.quota_tier, "IG_DAILY_QUOTA_FRESH")
    cap = min(getattr(settings, tier_attr), settings.IG_MAX_REQUESTS_PER_ACCOUNT_PER_DAY)
    row = session.execute(
        text(
            "SELECT calls_made FROM ig_usage_daily "
            "WHERE date = CURRENT_DATE AND account_id = :aid"
        ),
        {"aid": account.id},
    ).first()
    used = int(row[0]) if row else 0
    return cap - used


def acquire(session: Session, job: ScrapeJob) -> AcquiredAccount:
    """Pick one account for `job`. Raises NoAccountAvailable if none fit.

    Caller's transaction holds the row lock until release/rollback. The
    worker loop commits immediately after acquire so the lock window is
    short — we don't want to sit on a row while doing IG calls.
    """
    required_role = _required_role_for_job(job)
    rows: Iterable = session.execute(_CLAIM_SQL, {"required_role": required_role}).all()
    now = datetime.now(timezone.utc)

    for (account_id,) in rows:
        account = session.get(Account, account_id)
        if account is None:
            continue
        if not _within_active_hours(account, now):
            continue
        if _quota_remaining(session, account) <= 0:
            logger.info(
                "account_skipped_quota",
                account_id=str(account.id),
                tier=account.quota_tier,
            )
            continue

        proxy: Optional[Proxy] = None
        if account.proxy_id is not None:
            proxy = session.get(Proxy, account.proxy_id)

        account.last_used_at = now
        session.add(account)
        session.commit()
        logger.info(
            "account_acquired",
            account_id=str(account.id),
            username=account.username,
            job_id=str(job.id),
            quota_tier=account.quota_tier,
        )
        return AcquiredAccount(account=account, proxy=proxy)

    raise NoAccountAvailable(
        f"no available '{required_role}' account "
        f"(in active hours, with quota left, not in cooldown)"
    )


# ---------------------------------------------------------------------
# Release
# ---------------------------------------------------------------------


def _cooldown_seconds(outcome: ReleaseOutcome) -> Optional[int]:
    """Map an outcome to an account-level cooldown duration.

    Numbers come from § 5.3 (I) of the plan. Random jitter inside the
    range keeps multiple challenged accounts from re-emerging at the
    exact same moment.
    """
    if outcome == "rate_limited":
        return randint(2 * 3600, 4 * 3600)
    if outcome == "challenge":
        return None  # status flips to challenge_required, not just a cooldown
    if outcome == "soft_fail":
        return randint(
            settings.IG_ACCOUNT_COOLDOWN_MIN, settings.IG_ACCOUNT_COOLDOWN_MAX
        )
    if outcome == "fatal":
        return None  # account marked banned/disabled instead
    return None


def release(
    session: Session,
    acquired: Optional[AcquiredAccount],
    outcome: ReleaseOutcome,
    *,
    detail: Optional[str] = None,
) -> None:
    """Apply post-job state transitions to the account row.

    `success`         → reset failure_count, no cooldown.
    `soft_fail`       → bump failure_count, set cooldown 20–60 min.
    `rate_limited`    → cooldown 2–4 h.
    `challenge`       → status = challenge_required (human handoff).
    `fatal`           → status = banned (not retryable).
    """
    if acquired is None:
        return
    account = acquired.account
    if outcome == "success":
        account.failure_count = 0
        account.cooldown_until = None
    elif outcome == "soft_fail":
        account.failure_count = (account.failure_count or 0) + 1
        cd = _cooldown_seconds(outcome)
        if cd is not None:
            account.cooldown_until = datetime.now(timezone.utc) + timedelta(seconds=cd)
        if account.failure_count >= 5:
            account.status = "cooldown"
    elif outcome == "rate_limited":
        cd = _cooldown_seconds(outcome) or 7200
        account.cooldown_until = datetime.now(timezone.utc) + timedelta(seconds=cd)
        account.status = "cooldown"
    elif outcome == "challenge":
        account.status = "challenge_required"
    elif outcome == "fatal":
        account.status = "banned"

    account.updated_at = datetime.now(timezone.utc)
    session.add(account)
    session.commit()
    logger.info(
        "account_released",
        account_id=str(account.id),
        outcome=outcome,
        new_status=account.status,
        cooldown_until=account.cooldown_until.isoformat() if account.cooldown_until else None,
        detail=detail,
    )
