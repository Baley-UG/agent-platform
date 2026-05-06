"""Slack webhook endpoint — receives events from Slack and forwards them to Bolt."""

from fastapi import APIRouter, Request
from fastapi.responses import Response

from app.core.slack import slack_handler
from app.core.logging import logger

router = APIRouter()


@router.post("/events")
async def slack_events(request: Request) -> Response:
    """Receive all Slack events (mentions, DMs, URL verification).

    Slack sends a challenge on first setup — Bolt handles it automatically.

    Args:
        request: The raw FastAPI request from Slack.

    Returns:
        Response: An ack response expected by Slack (HTTP 200).
    """
    logger.info("slack_event_received", path=str(request.url))
    return await slack_handler.handle(request)
