"""Marketing API endpoints for TikTok Ads campaign management via AI agent.

This module provides chat endpoints backed by the TikTokMarketingAgent,
enabling natural language interaction with TikTok Ads Manager.
"""

import json

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
)
from fastapi.responses import StreamingResponse

from app.api.v1.auth import get_current_session
from app.core.config import settings
from app.core.langgraph.marketing_graph import TikTokMarketingAgent
from app.core.limiter import limiter
from app.core.logging import logger
from app.core.metrics import llm_stream_duration_seconds
from app.models.session import Session
from app.schemas.chat import (
    ChatRequest,
    ChatResponse,
    StreamResponse,
)

router = APIRouter()
marketing_agent = TikTokMarketingAgent()


@router.post("/chat", response_model=ChatResponse)
@limiter.limit(settings.RATE_LIMIT_ENDPOINTS["chat"][0])
async def marketing_chat(
    request: Request,
    chat_request: ChatRequest,
    session: Session = Depends(get_current_session),
):
    """Process a marketing chat request using the TikTok Marketing Agent.

    Args:
        request: The FastAPI request object for rate limiting.
        chat_request: The chat request containing messages.
        session: The current session from the auth token.

    Returns:
        ChatResponse: The processed chat response with marketing actions or insights.

    Raises:
        HTTPException: If there's an error processing the request.
    """
    try:
        logger.info(
            "marketing_chat_request_received",
            session_id=session.id,
            message_count=len(chat_request.messages),
        )

        result = await marketing_agent.get_response(
            chat_request.messages, session.id, user_id=session.user_id
        )

        logger.info("marketing_chat_request_processed", session_id=session.id)
        return ChatResponse(messages=result)
    except Exception as e:
        logger.error("marketing_chat_request_failed", session_id=session.id, error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/chat/stream")
@limiter.limit(settings.RATE_LIMIT_ENDPOINTS["chat_stream"][0])
async def marketing_chat_stream(
    request: Request,
    chat_request: ChatRequest,
    session: Session = Depends(get_current_session),
):
    """Process a marketing chat request with streaming response.

    Args:
        request: The FastAPI request object for rate limiting.
        chat_request: The chat request containing messages.
        session: The current session from the auth token.

    Returns:
        StreamingResponse: A streaming response of the marketing agent completion.

    Raises:
        HTTPException: If there's an error processing the request.
    """
    try:
        logger.info(
            "marketing_stream_request_received",
            session_id=session.id,
            message_count=len(chat_request.messages),
        )

        async def event_generator():
            try:
                with llm_stream_duration_seconds.labels(
                    model=marketing_agent.llm_service.get_llm().get_name()
                ).time():
                    async for chunk in marketing_agent.get_stream_response(
                        chat_request.messages, session.id, user_id=session.user_id
                    ):
                        response = StreamResponse(content=chunk, done=False)
                        yield f"data: {json.dumps(response.model_dump())}\n\n"

                final_response = StreamResponse(content="", done=True)
                yield f"data: {json.dumps(final_response.model_dump())}\n\n"
            except Exception as e:
                logger.error(
                    "marketing_stream_request_failed",
                    session_id=session.id,
                    error=str(e),
                    exc_info=True,
                )
                error_response = StreamResponse(content=str(e), done=True)
                yield f"data: {json.dumps(error_response.model_dump())}\n\n"

        return StreamingResponse(event_generator(), media_type="text/event-stream")
    except Exception as e:
        logger.error("marketing_stream_request_failed", session_id=session.id, error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/messages", response_model=ChatResponse)
@limiter.limit(settings.RATE_LIMIT_ENDPOINTS["messages"][0])
async def get_marketing_messages(
    request: Request,
    session: Session = Depends(get_current_session),
):
    """Get all marketing conversation messages for a session.

    Args:
        request: The FastAPI request object for rate limiting.
        session: The current session from the auth token.

    Returns:
        ChatResponse: All messages in the marketing session.

    Raises:
        HTTPException: If there's an error retrieving the messages.
    """
    try:
        messages = await marketing_agent.get_chat_history(session.id)
        return ChatResponse(messages=messages)
    except Exception as e:
        logger.error("get_marketing_messages_failed", session_id=session.id, error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/messages")
@limiter.limit(settings.RATE_LIMIT_ENDPOINTS["messages"][0])
async def clear_marketing_history(
    request: Request,
    session: Session = Depends(get_current_session),
):
    """Clear all marketing conversation history for a session.

    Args:
        request: The FastAPI request object for rate limiting.
        session: The current session from the auth token.

    Returns:
        dict: Confirmation message.
    """
    try:
        await marketing_agent.clear_chat_history(session.id)
        return {"message": "Marketing chat history cleared successfully"}
    except Exception as e:
        logger.error("clear_marketing_history_failed", session_id=session.id, error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
