"""Thin S3 wrapper for ad_scraper.

Shares the same bucket as content_pipeline and ig_scraper (env-driven).
We namespace under `ad-scraper/materials/<material_id>/` so the three
services don't fight over keys.

Lazy client construction — if S3 isn't configured (`AD_MIRROR_MEDIA=never`
or env unset), `is_configured()` returns False and callers should skip
mirroring rather than crash an ingestion job.
"""

from __future__ import annotations

import io
from typing import Optional

import boto3
from botocore.client import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import settings

KEY_PREFIX = "ad-scraper/materials"


class S3NotConfigured(RuntimeError):
    """Raised when an S3 op is attempted but env isn't fully populated."""


_client = None


def _make_client():
    if not (settings.S3_BUCKET and settings.S3_ACCESS_KEY and settings.S3_SECRET_KEY):
        raise S3NotConfigured("S3_BUCKET / S3_ACCESS_KEY / S3_SECRET_KEY must be set to mirror media")
    return boto3.client(
        "s3",
        endpoint_url=settings.S3_ENDPOINT,
        aws_access_key_id=settings.S3_ACCESS_KEY,
        aws_secret_access_key=settings.S3_SECRET_KEY,
        region_name=settings.S3_REGION,
        config=BotoConfig(
            signature_version="s3v4",
            s3={"addressing_style": "path" if settings.S3_USE_PATH_STYLE else "auto"},
        ),
    )


def client():
    """Memoised S3 client; raises `S3NotConfigured` if env is incomplete."""
    global _client
    if _client is None:
        _client = _make_client()
    return _client


def is_configured() -> bool:
    """Cheap check used by callers that should skip silently when off."""
    return bool(settings.S3_BUCKET and settings.S3_ACCESS_KEY and settings.S3_SECRET_KEY)


def make_material_key(material_id: str, filename: str) -> str:
    """Canonical layout: `ad-scraper/materials/<material_id>/<filename>`.

    `material_id` is the API's 32-hex id; we still sanitise it because it
    lands in an object key and we never want a caller-supplied `../`.
    """
    safe_id = "".join(c for c in str(material_id) if c.isalnum() or c in "-_")[:64] or "unknown"
    safe_name = filename.replace("/", "_").replace("\\", "_")[:120]
    return f"{KEY_PREFIX}/{safe_id}/{safe_name}"


def upload_bytes(key: str, data: bytes, content_type: Optional[str] = None) -> str:
    """Upload `data` under `key`; returns the key."""
    extra = {"ContentType": content_type} if content_type else {}
    client().upload_fileobj(io.BytesIO(data), settings.S3_BUCKET, key, ExtraArgs=extra or None)
    return key


def presigned_get_url(key: str, ttl: Optional[int] = None) -> str:
    """Short-lived GET URL so a private bucket can still be previewed."""
    ttl_s = int(ttl or settings.S3_PRESIGNED_URL_TTL_SECONDS)
    return client().generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.S3_BUCKET, "Key": key},
        ExpiresIn=ttl_s,
    )
