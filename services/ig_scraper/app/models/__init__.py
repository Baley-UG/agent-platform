"""SQLModel ORM models for the ig_scraper service.

Importing this module triggers registration of every table on
`SQLModel.metadata`, which Alembic uses for autogeneration and the
service uses for runtime queries.
"""

from app.models.account import Account
from app.models.audio import AudioTrack
from app.models.comment import Comment
from app.models.hashtag import Hashtag
from app.models.heartbeat import WorkerHeartbeat
from app.models.highlight import Highlight, HighlightItem
from app.models.job import ScrapeJob
from app.models.post import Post, PostHashtag, PostMetricSnapshot
from app.models.proxy import Proxy
from app.models.story import Story
from app.models.target import ScanTarget
from app.models.usage import UsageDaily
from app.models.user import IgUser
from app.models.webhook import Webhook
from app.models.webhook_delivery import WebhookDelivery

__all__ = [
    "Account",
    "AudioTrack",
    "Comment",
    "Hashtag",
    "Highlight",
    "HighlightItem",
    "IgUser",
    "Post",
    "PostHashtag",
    "PostMetricSnapshot",
    "Proxy",
    "ScanTarget",
    "ScrapeJob",
    "Story",
    "UsageDaily",
    "Webhook",
    "WebhookDelivery",
    "WorkerHeartbeat",
]
