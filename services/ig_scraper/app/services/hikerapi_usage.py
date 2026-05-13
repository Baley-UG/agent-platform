"""Real-time per-request HikerAPI usage counter.

Why this exists separately from `usage.py`:

`ig_usage_daily` is keyed on `account_id` and counts instagrapi-pathway
work (which requires acquiring an Instagram account). HikerAPI calls
never touch that path — they don't acquire an account — so the
account_id-keyed table is structurally wrong for them.

`ig_hikerapi_usage` is keyed on (date, path, status_code) and bumped
**inside the HTTP client**, BEFORE the response is parsed by callers.
That means a job that crashes mid-flight still leaves a paper trail,
which the old "increment on job success" pattern lost.

Each call opens its own short-lived transaction so a rollback in the
caller's transaction (e.g. scraper error after the API call) can't
erase the usage record. Cost: one extra commit per HikerAPI call.
Acceptable — the table is tiny and the alternative is invisible cost.
"""

from __future__ import annotations

from sqlalchemy import text

from app.core.logging import logger
from app.services.database import session_scope

_UPSERT = text(
    """
    INSERT INTO ig_hikerapi_usage (date, path, status_code, calls_total, updated_at)
    VALUES (CURRENT_DATE, :path, :status_code, 1, now())
    ON CONFLICT (date, path, status_code) DO UPDATE SET
        calls_total = ig_hikerapi_usage.calls_total + 1,
        updated_at  = now()
    """
)


def record_call(path: str, status_code: int) -> None:
    """Bump the (today, path, status_code) row by one.

    Never raises — a counter blip must not break a scrape. Path is
    truncated to 255 chars to fit the column.
    """
    try:
        with session_scope() as session:
            session.execute(
                _UPSERT,
                {"path": (path or "?")[:255], "status_code": int(status_code)},
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "hikerapi_usage_record_failed",
            path=path,
            status_code=status_code,
            error=str(exc),
        )
