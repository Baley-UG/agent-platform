"""Slack user link model.

Maps Slack user IDs to application user IDs for unified identity across channels.
"""

from sqlmodel import Field

from app.models.base import BaseModel


class SlackUserLink(BaseModel, table=True):
    """Link between a Slack user and an application user."""

    slack_user_id: str = Field(primary_key=True)
    user_id: int = Field(foreign_key="user.id")
    email: str = Field(default="")
