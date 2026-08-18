"""S3 client wrapper.

One boto3 code path covers both MinIO (dev) and Hetzner Object Storage (prod).
The only environment difference is `S3_USE_PATH_STYLE`: MinIO requires it,
real S3 / Hetzner accept virtual-host style.

All keys are namespaced under `projects/{project_id}/...` — see PLAN § 2.10
for the path layout.
"""

from __future__ import annotations

import io
import uuid
from typing import Optional

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from app.core.config import settings
from app.core.logging import logger


def _make_client():
    return boto3.client(
        "s3",
        endpoint_url=settings.S3_ENDPOINT,
        aws_access_key_id=settings.S3_ACCESS_KEY,
        aws_secret_access_key=settings.S3_SECRET_KEY,
        region_name=settings.S3_REGION,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path" if settings.S3_USE_PATH_STYLE else "virtual"},
            retries={"max_attempts": 3, "mode": "standard"},
        ),
    )


_client = None
_presign_client = None


def client():
    """Return a lazily-initialized boto3 S3 client."""
    global _client
    if _client is None:
        _client = _make_client()
    return _client


def presign_client():
    """Separate client used ONLY for generating presigned URLs.

    When `S3_PUBLIC_ENDPOINT` is set (typical dev: MinIO host port), we
    sign against that URL so the resulting links match the host the
    browser will use. Without this, SigV4 host-header binding makes the
    URL 403 the moment we rewrite the host string after signing.

    For uploads / downloads inside the worker, `client()` (above) still
    uses the internal endpoint.
    """
    global _presign_client
    if _presign_client is None:
        endpoint = (
            getattr(settings, "S3_PUBLIC_ENDPOINT", None) or settings.S3_ENDPOINT
        )
        _presign_client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=settings.S3_ACCESS_KEY,
            aws_secret_access_key=settings.S3_SECRET_KEY,
            region_name=settings.S3_REGION,
            config=Config(
                signature_version="s3v4",
                s3={
                    "addressing_style": "path" if settings.S3_USE_PATH_STYLE else "virtual"
                },
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )
    return _presign_client


def is_configured() -> bool:
    """Best-effort check that S3 credentials + bucket are populated.

    Used by callers that should skip S3 ops silently (no exception)
    when running with `S3_*` env unset — e.g. dev images that don't
    use MinIO or production deployments without provider keys.
    """
    return bool(settings.S3_BUCKET and settings.S3_ACCESS_KEY and settings.S3_SECRET_KEY)


def ensure_bucket() -> None:
    """Create the bucket if it doesn't exist (idempotent).

    MinIO normally pre-creates the bucket via the `minio_init` compose service,
    but tests and ad-hoc dev runs may need this.
    """
    s3 = client()
    bucket = settings.S3_BUCKET
    try:
        s3.head_bucket(Bucket=bucket)
        return
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code not in ("404", "NoSuchBucket", "NotFound"):
            raise
    s3.create_bucket(Bucket=bucket)
    logger.info("s3_bucket_created", bucket=bucket)


def project_prefix(project_id: uuid.UUID | str) -> str:
    """Return the canonical key prefix for a project's assets.

    `S3_ROOT_PREFIX` lets several services share one bucket by keeping
    each one under its own top-level folder (e.g. `agent_platform/`).
    Unset — the default — keeps the historical `projects/<id>/` layout.
    """
    root = (getattr(settings, "S3_ROOT_PREFIX", "") or "").strip("/")
    return f"{root}/projects/{project_id}/" if root else f"projects/{project_id}/"


def make_key(project_id: uuid.UUID | str, kind: str, filename: str) -> str:
    """Compose an S3 key like `projects/<id>/<kind>/<uuid>-<filename>`.

    `kind` is one of: scenarios, scenes, audio, finals, templates, music,
    brand_assets, references, thumbnails. (See PLAN § 2.10.)
    """
    safe_name = filename.replace("/", "_").replace("\\", "_")
    return f"{project_prefix(project_id)}{kind}/{uuid.uuid4()}-{safe_name}"


def upload_bytes(key: str, data: bytes, content_type: Optional[str] = None) -> str:
    """Upload bytes to S3 and return the key."""
    extra: dict = {}
    if content_type:
        extra["ContentType"] = content_type
    client().upload_fileobj(io.BytesIO(data), settings.S3_BUCKET, key, ExtraArgs=extra or None)
    return key


def presigned_put_url(key: str, content_type: Optional[str] = None, ttl: Optional[int] = None) -> str:
    """Generate a presigned PUT URL for direct browser/admin upload."""
    params = {"Bucket": settings.S3_BUCKET, "Key": key}
    if content_type:
        params["ContentType"] = content_type
    return client().generate_presigned_url(
        "put_object",
        Params=params,
        ExpiresIn=ttl or settings.S3_PRESIGNED_URL_TTL_SECONDS,
    )


def presigned_get_url(key: str, ttl: Optional[int] = None) -> str:
    """Generate a presigned GET URL for short-lived asset reads.

    Signs against `S3_PUBLIC_ENDPOINT` (when set) so the URL works from
    the browser without breaking SigV4's host-header binding.
    """
    return presign_client().generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.S3_BUCKET, "Key": key},
        ExpiresIn=ttl or settings.S3_PRESIGNED_URL_TTL_SECONDS,
    )


def copy_object(source_key: str, dest_key: str) -> str:
    """Server-side copy within the bucket. Returns `dest_key`.

    Used when importing an asset another service already mirrored (e.g.
    `ad_scraper` under `ad-scraper/materials/...`). The copy keeps the
    reference's lifecycle independent of the producing service — it can
    prune its own prefix without breaking our rows — and because S3 does
    the copy server-side, no bytes travel through this process.
    """
    client().copy_object(
        Bucket=settings.S3_BUCKET,
        CopySource={"Bucket": settings.S3_BUCKET, "Key": source_key},
        Key=dest_key,
    )
    return dest_key


def delete_object(key: str) -> None:
    client().delete_object(Bucket=settings.S3_BUCKET, Key=key)


def head_object(key: str) -> Optional[dict]:
    """Return the HEAD metadata for a key, or None if it doesn't exist."""
    try:
        return client().head_object(Bucket=settings.S3_BUCKET, Key=key)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
            return None
        raise


def public_url(key: str) -> str:
    """Return a non-presigned URL.

    Useful for storing in DB when the bucket is public or when callers will
    presign at read time.
    """
    endpoint = settings.S3_ENDPOINT.rstrip("/")
    bucket = settings.S3_BUCKET
    if settings.S3_USE_PATH_STYLE:
        return f"{endpoint}/{bucket}/{key}"
    # Virtual-host: https://<bucket>.<host>/<key>
    proto, _, host = endpoint.partition("://")
    return f"{proto}://{bucket}.{host}/{key}"
