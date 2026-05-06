"""Webhook subscription, dispatch, signing, retry.

Three pieces:
- Service-layer CRUD for `ig_webhooks` rows.
- `enqueue_delivery(...)` — written from scoring (post_score_threshold)
  and the worker (target_run_completed). Just inserts a pending row
  in `ig_webhook_deliveries`; actual HTTP happens in the dispatch loop.
- `fire_pending_deliveries(...)` — async helper run by the scheduler
  every IG_WEBHOOK_DISPATCH_INTERVAL_SECONDS. Claims pending rows
  via `FOR UPDATE SKIP LOCKED`, signs the body with HMAC-SHA256, POSTs
  it, persists the result. Failures bump `attempt` and reschedule with
  exponential backoff; reaching `max_attempts` flips status to
  `failed` and bumps the parent webhook's `consecutive_failures`.

Signing convention (matches GitHub / Stripe-style):
    Header `X-IG-Signature: sha256=<hex>` over the raw JSON body.
    Receiver verifies with the same secret.
"""

import asyncio
import hashlib
import hmac
import json
import math
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

import httpx
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.core.config import settings
from app.core.logging import logger
from app.models.webhook import Webhook

# Backoff schedule. Each retry waits longer; matches what most
# webhook receivers expect from a well-behaved sender.
_BACKOFF_BASE_SECONDS = 30
_BACKOFF_MAX_SECONDS = 60 * 60 * 6


# ----------------------------------------------------------------------
# Subscription CRUD
# ----------------------------------------------------------------------


VALID_EVENT_TYPES = frozenset({
    "post_score_threshold",
    "target_run_completed",
    "account_challenge_required",
})


class WebhookNotFoundError(Exception):
    """Raised when a lookup returns nothing."""


class InvalidWebhookError(Exception):
    """Raised on bad event_type / URL."""


def subscribe(
    session: Session,
    *,
    event_type: str,
    url: str,
    secret: Optional[str] = None,
    filters: Optional[Dict[str, Any]] = None,
) -> Webhook:
    """Insert a new subscription. Idempotency: caller chooses URL."""
    if event_type not in VALID_EVENT_TYPES:
        raise InvalidWebhookError(
            f"event_type must be one of {sorted(VALID_EVENT_TYPES)}"
        )
    if not url.startswith(("http://", "https://")):
        raise InvalidWebhookError("url must be http(s)://...")
    webhook = Webhook(
        event_type=event_type,
        url=url,
        secret=secret,
        filters=filters,
        status="active",
    )
    session.add(webhook)
    session.flush()
    logger.info(
        "webhook_subscribed",
        webhook_id=str(webhook.id),
        event_type=event_type,
        url=url,
    )
    return webhook


def list_webhooks(session: Session) -> List[Webhook]:
    return list(session.exec(select(Webhook).order_by(Webhook.created_at.desc())).all())


def delete_webhook(session: Session, webhook_id: uuid.UUID) -> None:
    """Hard-delete; we don't soft-delete since the receiver will simply
    not get any more events and that's the desired behaviour."""
    webhook = session.get(Webhook, webhook_id)
    if webhook is None:
        raise WebhookNotFoundError(str(webhook_id))
    session.delete(webhook)
    session.flush()
    logger.info("webhook_deleted", webhook_id=str(webhook_id))


# ----------------------------------------------------------------------
# Enqueue (called from scoring + worker)
# ----------------------------------------------------------------------

_INSERT_DELIVERY = text(
    """
    INSERT INTO ig_webhook_deliveries
        (id, webhook_id, event_type, payload, status, attempt, max_attempts,
         scheduled_for, created_at)
    VALUES
        (gen_random_uuid(), :webhook_id, :event_type, CAST(:payload AS jsonb),
         'pending', 0, :max_attempts, :now, :now)
    """
)


def _matches_filters(webhook_filters: Optional[Dict[str, Any]], payload: Dict[str, Any]) -> bool:
    """Cheap subset check: every key in `webhook_filters` must equal
    the corresponding value in `payload`. None or empty filters always
    match.

    For score-threshold subscriptions the filter looks like
    `{"min_score": 80}`, which we evaluate specially below.
    """
    if not webhook_filters:
        return True
    if "min_score" in webhook_filters:
        candidate = payload.get("score")
        if candidate is None or float(candidate) < float(webhook_filters["min_score"]):
            return False
    for key, expected in webhook_filters.items():
        if key == "min_score":
            continue
        if payload.get(key) != expected:
            return False
    return True


def find_matching_webhooks(
    session: Session, *, event_type: str, payload: Dict[str, Any]
) -> List[Webhook]:
    """Return active webhooks whose event_type + filters match the payload."""
    rows = session.exec(
        select(Webhook).where(
            Webhook.event_type == event_type, Webhook.status == "active"
        )
    ).all()
    return [w for w in rows if _matches_filters(w.filters, payload)]


def enqueue_delivery(
    session: Session, *, event_type: str, payload: Dict[str, Any]
) -> int:
    """Materialise a pending row per matching webhook. Returns the count.

    Caller's transaction commits as usual; the dispatcher polls the
    table on its own cadence.
    """
    matches = find_matching_webhooks(session, event_type=event_type, payload=payload)
    if not matches:
        return 0
    now = datetime.now(timezone.utc)
    body = json.dumps(payload, default=str)
    for webhook in matches:
        session.execute(
            _INSERT_DELIVERY,
            {
                "webhook_id": webhook.id,
                "event_type": event_type,
                "payload": body,
                "max_attempts": 5,
                "now": now,
            },
        )
    return len(matches)


# ----------------------------------------------------------------------
# Dispatch loop helpers
# ----------------------------------------------------------------------


def _backoff_seconds(attempt: int) -> int:
    """Exponential backoff capped at IG_WEBHOOK_BACKOFF_MAX_SECONDS."""
    seconds = _BACKOFF_BASE_SECONDS * (2 ** max(attempt - 1, 0))
    return min(seconds, _BACKOFF_MAX_SECONDS)


def _sign(secret: Optional[str], body: bytes) -> Optional[str]:
    """HMAC-SHA256 hex digest. None when no secret is configured."""
    if not secret:
        return None
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


_CLAIM_SQL = text(
    """
    UPDATE ig_webhook_deliveries
    SET status = 'in_flight',
        last_attempt_at = now(),
        attempt = attempt + 1
    WHERE id IN (
        SELECT id FROM ig_webhook_deliveries
        WHERE status = 'pending' AND scheduled_for <= now()
        ORDER BY scheduled_for ASC
        FOR UPDATE SKIP LOCKED
        LIMIT :batch
    )
    RETURNING id, webhook_id, event_type, payload, attempt, max_attempts
    """
)


def _claim_pending(session: Session, batch: int) -> List[Dict[str, Any]]:
    rows = session.execute(_CLAIM_SQL, {"batch": batch}).mappings().all()
    session.commit()
    return [dict(r) for r in rows]


async def _post_one(
    *, url: str, body: bytes, signature: Optional[str], event_type: str
) -> tuple[Optional[int], Optional[str]]:
    """Single HTTP POST with a short timeout. Returns (status_code, error)."""
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "ig-scraper-webhooks/1.0",
        "X-IG-Event": event_type,
    }
    if signature:
        headers["X-IG-Signature"] = signature
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(url, content=body, headers=headers)
            return response.status_code, None
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"


async def fire_pending_deliveries(*, batch: int = 25) -> int:
    """Single dispatch pass — async, intended for the scheduler loop.

    Returns the number of deliveries it touched (delivered or rescheduled).
    """
    from app.services.database import session_scope

    with session_scope() as session:
        pending = _claim_pending(session, batch)
    if not pending:
        return 0

    # Hydrate URL + secret per delivery in one extra read so we don't
    # pull them into the claim transaction and keep that lock window short.
    with session_scope() as session:
        webhook_rows = {
            w.id: w
            for w in session.exec(
                select(Webhook).where(
                    Webhook.id.in_([d["webhook_id"] for d in pending])  # type: ignore[arg-type]
                )
            ).all()
        }

    handled = 0
    async def _process(delivery: Dict[str, Any]) -> None:
        nonlocal handled
        webhook = webhook_rows.get(delivery["webhook_id"])
        if webhook is None:
            return  # subscription deleted while we were claiming
        body = json.dumps(delivery["payload"], default=str).encode("utf-8")
        signature = _sign(webhook.secret, body)
        status_code, error = await _post_one(
            url=webhook.url,
            body=body,
            signature=signature,
            event_type=delivery["event_type"],
        )
        ok = status_code is not None and 200 <= status_code < 300

        with session_scope() as session:
            if ok:
                session.execute(
                    text(
                        "UPDATE ig_webhook_deliveries "
                        "SET status='delivered', response_status=:s, error=NULL "
                        "WHERE id=:id"
                    ),
                    {"s": status_code, "id": delivery["id"]},
                )
                session.execute(
                    text(
                        "UPDATE ig_webhooks SET last_delivery_at=now(), "
                        "last_delivery_status=:s, consecutive_failures=0 WHERE id=:id"
                    ),
                    {"s": status_code, "id": delivery["webhook_id"]},
                )
            else:
                attempt = delivery["attempt"]
                if attempt >= delivery["max_attempts"]:
                    new_status = "failed"
                    next_at = None
                else:
                    new_status = "pending"
                    next_at = datetime.now(timezone.utc) + timedelta(
                        seconds=_backoff_seconds(attempt)
                    )
                session.execute(
                    text(
                        """
                        UPDATE ig_webhook_deliveries
                        SET status         = :new_status,
                            response_status= :rs,
                            error          = :err,
                            scheduled_for  = COALESCE(:next_at, scheduled_for)
                        WHERE id = :id
                        """
                    ),
                    {
                        "new_status": new_status,
                        "rs": status_code,
                        "err": (error or f"HTTP {status_code}")[:500],
                        "next_at": next_at,
                        "id": delivery["id"],
                    },
                )
                session.execute(
                    text(
                        """
                        UPDATE ig_webhooks
                        SET consecutive_failures = consecutive_failures + 1,
                            status = CASE
                                WHEN consecutive_failures + 1 >= 10 THEN 'failing'
                                ELSE status
                            END
                        WHERE id = :id
                        """
                    ),
                    {"id": delivery["webhook_id"]},
                )
        handled += 1

    await asyncio.gather(*(_process(d) for d in pending))
    if handled:
        logger.info("webhook_dispatch_pass", deliveries=handled)
    return handled
