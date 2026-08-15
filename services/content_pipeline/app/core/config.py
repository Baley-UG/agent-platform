"""Application configuration for content_pipeline.

Mirrors the env-loading pattern from `ig_scraper`: service-local `.env`
files take priority, then repo-root. Reuses the shared `POSTGRES_*` keys
so we land in the same database as the rest of the platform.
"""

import os
from enum import Enum
from pathlib import Path
from typing import ClassVar, List, Optional

from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(str, Enum):
    """Application environment types."""

    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"
    TEST = "test"


def _load_env_files() -> None:
    """Load env files in priority order (service-local first, then repo root)."""
    env_name = os.getenv("APP_ENV", "development").lower()
    config_path = Path(__file__).resolve()
    candidates: list[Path] = []

    try:
        service_dir = config_path.parents[2]
        candidates.extend(
            [
                service_dir / f".env.{env_name}.local",
                service_dir / f".env.{env_name}",
                service_dir / ".env.local",
                service_dir / ".env",
            ]
        )
    except IndexError:
        pass

    try:
        repo_root = config_path.parents[4]
        candidates.extend(
            [
                repo_root / f".env.{env_name}.local",
                repo_root / f".env.{env_name}",
                repo_root / ".env.local",
                repo_root / ".env",
            ]
        )
    except IndexError:
        pass

    for candidate in candidates:
        try:
            if candidate.is_file():
                load_dotenv(dotenv_path=candidate, override=False)
        except OSError:
            continue


_load_env_files()


class Settings(BaseSettings):
    """content_pipeline service settings."""

    model_config = SettingsConfigDict(env_file=None, case_sensitive=True, extra="ignore")

    # ----- Service identity (constants — avoid clashes with main app env) -----
    PROJECT_NAME: ClassVar[str] = "content-pipeline"
    VERSION: ClassVar[str] = "0.1.0"
    DESCRIPTION: ClassVar[str] = "AI content production pipeline."
    API_V1_STR: ClassVar[str] = "/api/v1"

    ENVIRONMENT: Environment = Field(default=Environment.DEVELOPMENT, validation_alias="APP_ENV")

    # ----- Auth -----
    CP_API_KEY: str = Field(default="changeme-not-a-real-key")
    CP_SECRET_KEY: str = Field(
        default="changeme-fernet-key",
        description="Fernet key for encrypting social_accounts.credentials_encrypted. "
        "Generate with `python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'`.",
    )

    # ----- CORS -----
    ALLOWED_ORIGINS: str = Field(default="*")

    # ----- Postgres (shared with agent-platform) -----
    POSTGRES_HOST: str = Field(default="db")
    POSTGRES_PORT: int = Field(default=5432)
    POSTGRES_USER: str = Field(default="postgres")
    POSTGRES_PASSWORD: str = Field(default="postgres")
    POSTGRES_DB: str = Field(default="agent_platform")
    POSTGRES_POOL_SIZE: int = Field(default=10)
    POSTGRES_MAX_OVERFLOW: int = Field(default=20)

    POSTGRES_READ_REPLICA_DSN: Optional[str] = Field(default=None)

    # ----- Redis (shared with ig_scraper) -----
    REDIS_HOST: str = Field(default="redis")
    REDIS_PORT: int = Field(default=6379)
    REDIS_DB: int = Field(default=1, description="Distinct DB index from ig_scraper to avoid key collisions.")
    REDIS_PASSWORD: Optional[str] = Field(default=None)

    # ----- S3 storage (MinIO in dev, Hetzner Object Storage in prod) -----
    S3_ENDPOINT: str = Field(default="http://minio:9000")
    # Browser-facing host for presigned GET URLs. The internal endpoint
    # above is what the worker uses to talk to S3 (docker DNS); this
    # value is swapped in on the way out so the user's browser can
    # actually fetch the URL. Dev default: MinIO host port. In prod
    # leave unset — `S3_ENDPOINT` is already public.
    S3_PUBLIC_ENDPOINT: Optional[str] = Field(default="http://localhost:9000")
    S3_BUCKET: str = Field(default="content-pipeline-dev")
    # Top-level folder inside the bucket, so this service can share a
    # bucket with others (e.g. "agent_platform"). Empty = bucket root.
    S3_ROOT_PREFIX: str = Field(default="")
    S3_ACCESS_KEY: str = Field(default="minioadmin")
    S3_SECRET_KEY: str = Field(default="minioadmin")
    S3_REGION: str = Field(default="us-east-1")
    # MinIO requires path-style addressing; Hetzner / AWS use virtual-host style.
    S3_USE_PATH_STYLE: bool = Field(default=True)
    S3_PRESIGNED_URL_TTL_SECONDS: int = Field(default=3600)

    # ----- Worker / scheduler concurrency -----
    CP_WORKER_CONCURRENCY: int = Field(default=2)
    CP_SCHEDULER_TICK_SECONDS: int = Field(default=60)

    # ----- API host port (override when 8082 is taken) -----
    CP_HOST_PORT: int = Field(default=8082)

    # ----- Provider auth (concrete clients land in CP-M2..CP-M5) -----
    # Model selection itself lives in `model_routes` (admin-editable);
    # here we only carry the secret used when the route's provider matches.
    OPENROUTER_API_KEY: Optional[str] = Field(default=None)
    OPENROUTER_BASE_URL: str = Field(default="https://openrouter.ai/api/v1")
    OPENROUTER_HTTP_REFERER: Optional[str] = Field(
        default=None,
        description="Optional X-Title / HTTP-Referer headers help OpenRouter dashboards group calls.",
    )

    FAL_KEY: Optional[str] = Field(default=None)
    SEEDANCE_API_KEY: Optional[str] = Field(default=None)
    ELEVENLABS_API_KEY: Optional[str] = Field(default=None)

    # Analyzer knobs
    CP_ANALYZER_MAX_KEYFRAMES: int = Field(default=8)
    CP_ANALYZER_HTTP_TIMEOUT_SECONDS: float = Field(default=120.0)

    # ----- Auth (CP-M8.5) -----
    # JWT signing secret. Separate from CP_SECRET_KEY (which is Fernet for
    # social_account credentials at rest) so we can rotate them independently.
    CP_JWT_SECRET: str = Field(default="changeme-jwt-secret")
    CP_JWT_ALGORITHM: str = Field(default="HS256")
    CP_ACCESS_TOKEN_TTL_MINUTES: int = Field(default=60)
    CP_REFRESH_TOKEN_TTL_DAYS: int = Field(default=7)

    # Bootstrap admin — created on first migration run if no users exist.
    # Leave blank in dev; set explicitly in prod via .env.
    CP_BOOTSTRAP_ADMIN_EMAIL: Optional[str] = Field(default=None)
    CP_BOOTSTRAP_ADMIN_PASSWORD: Optional[str] = Field(default=None)
    CP_BOOTSTRAP_ADMIN_NAME: Optional[str] = Field(default="Admin")

    # Video generation knobs (Seedance / Kling / Runway are async with polling)
    CP_VIDEO_GEN_TIMEOUT_SECONDS: float = Field(
        default=600.0,
        description="Hard wall-clock cap on a single I2V job's polling loop.",
    )

    @property
    def postgres_dsn(self) -> str:
        """Primary Postgres DSN (psycopg driver)."""
        return (
            f"postgresql+psycopg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    @property
    def redis_url(self) -> str:
        """Redis URL for RQ."""
        auth = f":{self.REDIS_PASSWORD}@" if self.REDIS_PASSWORD else ""
        return f"redis://{auth}{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB}"

    @property
    def allowed_origins(self) -> List[str]:
        """Parse the comma-separated CORS origin list."""
        raw = self.ALLOWED_ORIGINS.strip()
        if raw in ("", "*"):
            return ["*"]
        return [origin.strip() for origin in raw.split(",") if origin.strip()]


settings = Settings()
