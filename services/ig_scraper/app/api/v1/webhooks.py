"""Webhooks router — subscribe / list / delete (M9)."""

import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.api.v1.deps import require_api_key
from app.services import webhooks as webhooks_service
from app.services.database import session_scope

router = APIRouter(dependencies=[Depends(require_api_key)])


class WebhookCreate(BaseModel):
    """Body for POST /webhooks."""

    event_type: str = Field(description="post_score_threshold | target_run_completed | account_challenge_required")
    url: str
    secret: Optional[str] = Field(default=None, description="HMAC-SHA256 signing secret.")
    filters: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Equality filters; for score events use `{\"min_score\": 80}`.",
    )


class WebhookRead(BaseModel):
    id: uuid.UUID
    event_type: str
    url: str
    has_secret: bool
    filters: Optional[Dict[str, Any]]
    status: str
    consecutive_failures: int


def _to_read(w) -> WebhookRead:
    return WebhookRead(
        id=w.id,
        event_type=w.event_type,
        url=w.url,
        has_secret=bool(w.secret),
        filters=w.filters,
        status=w.status,
        consecutive_failures=w.consecutive_failures,
    )


@router.post("", response_model=WebhookRead, status_code=status.HTTP_201_CREATED)
def subscribe(payload: WebhookCreate) -> WebhookRead:
    try:
        with session_scope() as session:
            return _to_read(
                webhooks_service.subscribe(
                    session,
                    event_type=payload.event_type,
                    url=payload.url,
                    secret=payload.secret,
                    filters=payload.filters,
                )
            )
    except webhooks_service.InvalidWebhookError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.get("", response_model=List[WebhookRead])
def list_webhooks() -> List[WebhookRead]:
    with session_scope() as session:
        return [_to_read(w) for w in webhooks_service.list_webhooks(session)]


@router.delete("/{webhook_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_webhook(webhook_id: uuid.UUID) -> None:
    try:
        with session_scope() as session:
            webhooks_service.delete_webhook(session, webhook_id)
    except webhooks_service.WebhookNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="webhook not found")
