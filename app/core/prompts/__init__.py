"""This file contains the prompts for the agent."""

import os
from datetime import datetime

from app.core.config import settings


def load_system_prompt(**kwargs):
    """Load the system prompt from the file."""
    with open(os.path.join(os.path.dirname(__file__), "system.md"), "r") as f:
        return f.read().format(
            agent_name=settings.PROJECT_NAME + " Agent",
            current_date_and_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            **kwargs,
        )


def load_marketing_prompt(**kwargs):
    """Load the TikTok marketing agent system prompt from the file."""
    kwargs.setdefault("strategy_context", "")
    kwargs.setdefault("consensus_note", "")
    with open(os.path.join(os.path.dirname(__file__), "marketing.md"), "r") as f:
        return f.read().format(
            agent_name=settings.PROJECT_NAME + " Marketing Agent",
            current_date_and_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            **kwargs,
        )
