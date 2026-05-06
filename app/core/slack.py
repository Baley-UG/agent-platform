"""Slack integration for the AI agents.

This module wires up Slack Bolt (async) with the existing LangGraph agents.
It handles two routing patterns:

  - @bot mention in any channel   → General assistant (LangGraphAgent)
  - Message in #marketing channel → TikTok Marketing Agent

The Slack app runs in HTTP mode: Slack sends events to /slack/events,
which FastAPI forwards to the Bolt handler via SlackRequestHandler.
"""

from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.fastapi.async_handler import AsyncSlackRequestHandler
from secrets import token_urlsafe

from app.core.config import settings
from app.core.langgraph.graph import LangGraphAgent
from app.core.langgraph.marketing_graph import TikTokMarketingAgent
from app.core.logging import logger
from app.schemas.chat import Message
from app.models.user import User
from app.services.database import DatabaseService
from app.utils.sanitization import sanitize_email

# ── Agent singletons (shared with the REST API) ──────────────────────────────
_general_agent = LangGraphAgent()
_marketing_agent = TikTokMarketingAgent()
_db_service = DatabaseService()

# ── Slack Bolt async app ──────────────────────────────────────────────────────
slack_app = AsyncApp(
    token=settings.SLACK_BOT_TOKEN,
    signing_secret=settings.SLACK_SIGNING_SECRET,
)

# FastAPI adapter – used in the router
slack_handler = AsyncSlackRequestHandler(slack_app)

# ── Helpers ───────────────────────────────────────────────────────────────────

MARKETING_CHANNEL_NAMES = {"marketing", "tiktok-ads", "tiktok_ads", "ads", "paid-social"}


def _is_marketing_channel(channel_name: str) -> bool:
    """Return True if the channel name signals marketing intent."""
    return channel_name.lower().strip("#") in MARKETING_CHANNEL_NAMES


def _strip_bot_mention(text: str, bot_user_id: str) -> str:
    """Remove the @bot mention prefix from message text."""
    mention = f"<@{bot_user_id}>"
    return text.replace(mention, "").strip()


async def _resolve_channel_name(client, channel_id: str) -> str:
    """Resolve a channel ID to its human-readable name."""
    try:
        info = await client.conversations_info(channel=channel_id)
        return info["channel"].get("name", "")
    except Exception:
        return ""


async def _resolve_or_create_user(client, slack_user_id: str) -> tuple[int, str]:
    """Link Slack user to app user by email; create user if missing.

    Returns (app_user_id, session_id_prefix)
    """

    # Session id: keep Slack-scoped to preserve channel memory separation
    session_id = f"slack:{slack_user_id}"

    # Already linked?
    link = await _db_service.get_slack_link(slack_user_id)
    if link:
        return link.user_id, session_id

    # Fetch email from Slack profile
    try:
        info = await client.users_info(user=slack_user_id)
        email = info["user"]["profile"].get("email") or ""
    except Exception as e:
        logger.warning("slack_user_info_failed", slack_user_id=slack_user_id, error=str(e))
        # No email → fall back to anonymous user creation prevented; use synthetic email
        email = ""

    app_user = None
    sanitized_email = None
    if email:
        try:
            sanitized_email = sanitize_email(email)
            app_user = await _db_service.get_user_by_email(sanitized_email)
        except Exception as e:
            logger.warning("slack_email_sanitization_failed", slack_user_id=slack_user_id, email=email, error=str(e))

    if not app_user:
        # Create user with synthetic password; Slack auth is trusted path here
        # If no email, create synthetic unique email to satisfy DB uniqueness
        if not sanitized_email:
            sanitized_email = f"slack_{slack_user_id}@auto.local"

        hashed_pw = User.hash_password(token_urlsafe(16))
        app_user = await _db_service.create_user(email=sanitized_email, password=hashed_pw)
        logger.info("slack_user_created", slack_user_id=slack_user_id, email=sanitized_email, user_id=app_user.id)

    await _db_service.create_or_update_slack_link(slack_user_id=slack_user_id, user_id=app_user.id, email=sanitized_email or "")
    return app_user.id, session_id


# ── Event handlers ────────────────────────────────────────────────────────────

@slack_app.event("app_mention")
async def handle_app_mention(event: dict, client, say):
    """Respond to @bot mentions in any channel.

    Routes to the marketing agent if the channel is marketing-related,
    otherwise uses the general assistant agent.
    """
    channel_id: str = event.get("channel", "")
    slack_user_id: str = event.get("user", "")
    raw_text: str = event.get("text", "")
    thread_ts: str = event.get("thread_ts") or event.get("ts", "")

    bot_info = await client.auth_test()
    bot_user_id = bot_info.get("user_id", "")
    text = _strip_bot_mention(raw_text, bot_user_id)

    if not text:
        await say(text="Hello, How can I help you?", thread_ts=thread_ts)
        return

    channel_name = await _resolve_channel_name(client, channel_id)
    use_marketing = _is_marketing_channel(channel_name)
    agent = _marketing_agent if use_marketing else _general_agent

    app_user_id, session_id = await _resolve_or_create_user(client, slack_user_id)

    logger.info(
        "slack_mention_received",
        slack_user_id=slack_user_id,
        user_id=app_user_id,
        channel_id=channel_id,
        channel_name=channel_name,
        agent="marketing" if use_marketing else "general",
    )

    messages = [Message(role="user", content=text)]
    try:
        await say(text="_Düşünüyorum..._", thread_ts=thread_ts)
        result = await agent.get_response(messages, session_id=session_id, user_id=app_user_id)
        reply = next(
            (m.content for m in reversed(result) if m.role == "assistant"),
            "Bir yanıt oluşturulamadı.",
        )
        await say(text=reply, thread_ts=thread_ts)
        logger.info("slack_mention_replied", slack_user_id=slack_user_id, user_id=app_user_id, agent="marketing" if use_marketing else "general")
    except Exception as e:
        logger.exception("slack_mention_failed", slack_user_id=slack_user_id, user_id=app_user_id, error=str(e))
        await say(text=f"Üzgünüm, bir hata oluştu: {e}", thread_ts=thread_ts)


@slack_app.event("message")
async def handle_direct_message(event: dict, client, say):
    """Respond to direct messages sent to the bot.

    Only processes DMs (channel_type == 'im') to avoid double-processing
    channel messages that are also covered by app_mention.
    """
    if event.get("channel_type") != "im":
        return
    if event.get("subtype"):  # ignore bot messages, edits, etc.
        return

    slack_user_id: str = event.get("user", "")
    text: str = event.get("text", "").strip()
    thread_ts: str = event.get("ts", "")

    if not text:
        return

    app_user_id, session_id = await _resolve_or_create_user(client, slack_user_id)

    logger.info("slack_dm_received", slack_user_id=slack_user_id, user_id=app_user_id)

    messages = [Message(role="user", content=text)]
    try:
        result = await _general_agent.get_response(messages, session_id=session_id, user_id=app_user_id)
        reply = next(
            (m.content for m in reversed(result) if m.role == "assistant"),
            "Bir yanıt oluşturulamadı.",
        )
        await say(text=reply, thread_ts=thread_ts)
        logger.info("slack_dm_replied", slack_user_id=slack_user_id, user_id=app_user_id)
    except Exception as e:
        logger.exception("slack_dm_failed", slack_user_id=slack_user_id, user_id=app_user_id, error=str(e))
        await say(text=f"Üzgünüm, bir hata oluştu: {e}", thread_ts=thread_ts)
