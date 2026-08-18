"""Structured logging setup using structlog.

Mirrors ig_scraper and the agent-platform main service: JSON logs in
production, human-readable in development.
"""

import logging
import sys

import structlog

from app.core.config import Environment, settings


def _configure() -> structlog.stdlib.BoundLogger:
    """Configure structlog and stdlib logging once at import time."""
    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        timestamper,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if settings.ENVIRONMENT == Environment.DEVELOPMENT:
        renderer = structlog.dev.ConsoleRenderer(colors=True)
    else:
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=shared_processors + [renderer],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Tame noisy stdlib loggers.
    log_level = logging.DEBUG if settings.ENVIRONMENT == Environment.DEVELOPMENT else logging.INFO
    logging.basicConfig(level=log_level, stream=sys.stdout, format="%(message)s")
    for noisy in ("uvicorn.access", "uvicorn.error", "sqlalchemy.engine", "httpx", "httpcore", "botocore", "boto3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return structlog.get_logger("ad_scraper")


logger = _configure()
