"""Application configuration.

Reads env vars (with .env file fallback) for the ig_scraper service.
Reuses the same POSTGRES_* keys as the main agent-platform so both can
talk to the same database without duplicating credentials.
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
    """Load env files in priority order (service-local first, then repo root).

    Two layouts exist:
    - **Dev checkout**: `…/agent-platform/services/ig_scraper/app/core/config.py`
      → `parents[2]` is the service dir, `parents[4]` is the repo root.
    - **Container** (`/app/app/core/config.py`): `parents[2]` is `/app`,
      no repo root available. docker-compose's `env_file:` directive
      already injected env vars before this code runs, so missing
      files here are fine.

    Defensive: any path resolution that fails is swallowed — env vars
    set by docker-compose / the host shell still take effect.
    """
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
        # Container layout — no repo root to check; env_file already loaded.
        pass

    for candidate in candidates:
        try:
            if candidate.is_file():
                load_dotenv(dotenv_path=candidate, override=False)
        except OSError:
            continue


_load_env_files()


class Settings(BaseSettings):
    """ig_scraper service settings.

    All `IG_*` knobs default to safe production values; tighten via env
    when challenge rates climb.
    """

    model_config = SettingsConfigDict(env_file=None, case_sensitive=True, extra="ignore")

    # ----- Service identity (constants, NOT read from env to avoid clashing
    # with PROJECT_NAME/VERSION env vars used by the main agent-platform app) -----
    PROJECT_NAME: ClassVar[str] = "ig-scraper"
    VERSION: ClassVar[str] = "0.1.0"
    DESCRIPTION: ClassVar[str] = "Instagram competitor scraper microservice."
    API_V1_STR: ClassVar[str] = "/api/v1"

    ENVIRONMENT: Environment = Field(default=Environment.DEVELOPMENT, validation_alias="APP_ENV")

    # ----- Auth -----
    IG_SCRAPER_API_KEY: str = Field(default="changeme-not-a-real-key")
    IG_SECRET_KEY: str = Field(
        default="changeme-fernet-key",
        description="Fernet key for encrypting account passwords and proxy creds. "
        "Generate with `python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'`.",
    )

    # ----- CORS / rate limiting -----
    # Stored as a raw comma-separated (or JSON) string so pydantic-settings
    # doesn't try to JSON-decode it at the source layer. Use the
    # `allowed_origins` property for the parsed list.
    ALLOWED_ORIGINS: str = Field(default="*")

    # ----- Postgres (shared with agent-platform) -----
    POSTGRES_HOST: str = Field(default="db")
    POSTGRES_PORT: int = Field(default=5432)
    POSTGRES_USER: str = Field(default="postgres")
    POSTGRES_PASSWORD: str = Field(default="postgres")
    POSTGRES_DB: str = Field(default="agent_platform")
    POSTGRES_POOL_SIZE: int = Field(default=10)
    POSTGRES_MAX_OVERFLOW: int = Field(default=20)

    # Optional read-replica DSN for BI / analytical queries.
    # If unset, reads fall back to the primary connection.
    POSTGRES_READ_REPLICA_DSN: Optional[str] = Field(default=None)

    # ----- Worker / scheduler concurrency -----
    IG_WORKER_CONCURRENCY: int = Field(default=2)
    IG_SCHEDULER_TICK_SECONDS: int = Field(default=60)
    IG_HEARTBEAT_INTERVAL_SECONDS: int = Field(default=10)
    IG_HEARTBEAT_STALE_AFTER_SECONDS: int = Field(default=60)

    # A job whose worker died mid-process stays stuck in `status='running'`
    # forever (the worker only claims `queued` jobs). The reaper in the
    # scheduler resets running jobs older than this threshold back to
    # `queued`. Set higher than the longest real-world scrape — 60 min
    # comfortably covers 2000-post backfills with throttling.
    IG_JOB_STUCK_AFTER_MINUTES: int = Field(default=60)
    IG_REAPER_INTERVAL_SECONDS: int = Field(default=60)

    # ----- Anti-detection / throttling (see plan § 5.3) -----
    # Per-action delay tiers (lognormal-clipped).
    IG_DELAY_FEED_MIN: float = Field(default=4.0)
    IG_DELAY_FEED_MAX: float = Field(default=10.0)
    IG_DELAY_POST_MIN: float = Field(default=6.0)
    IG_DELAY_POST_MAX: float = Field(default=14.0)
    IG_DELAY_PROFILE_MIN: float = Field(default=5.0)
    IG_DELAY_PROFILE_MAX: float = Field(default=12.0)
    IG_DELAY_STORY_MIN: float = Field(default=3.0)
    IG_DELAY_STORY_MAX: float = Field(default=8.0)
    IG_DELAY_HASHTAG_MIN: float = Field(default=7.0)
    IG_DELAY_HASHTAG_MAX: float = Field(default=15.0)
    IG_DELAY_LOGIN_MIN: float = Field(default=20.0)
    IG_DELAY_LOGIN_MAX: float = Field(default=40.0)
    IG_MICRO_JITTER_MIN: float = Field(default=0.5)
    IG_MICRO_JITTER_MAX: float = Field(default=2.0)

    # Macro pauses.
    IG_MACRO_PAUSE_EVERY_MIN: int = Field(default=8)
    IG_MACRO_PAUSE_EVERY_MAX: int = Field(default=20)
    IG_MACRO_PAUSE_SECONDS_MIN: float = Field(default=30.0)
    IG_MACRO_PAUSE_SECONDS_MAX: float = Field(default=180.0)
    IG_LONG_BREAK_PROBABILITY: float = Field(default=0.33)
    IG_LONG_BREAK_SECONDS_MIN: float = Field(default=300.0)
    IG_LONG_BREAK_SECONDS_MAX: float = Field(default=900.0)

    # Session caps & cooldown.
    IG_SESSION_MAX_MINUTES: int = Field(default=25)
    IG_SESSION_MAX_CALLS: int = Field(default=300)
    IG_ACCOUNT_COOLDOWN_MIN: int = Field(default=1200)
    IG_ACCOUNT_COOLDOWN_MAX: int = Field(default=3600)

    # Tiered daily quotas.
    IG_DAILY_QUOTA_FRESH: int = Field(default=250)
    IG_DAILY_QUOTA_MID: int = Field(default=800)
    IG_DAILY_QUOTA_WARM: int = Field(default=1500)
    IG_MAX_REQUESTS_PER_ACCOUNT_PER_DAY: int = Field(default=1500, description="Hard ceiling above tier quotas.")
    IG_WARMUP_HOURS: int = Field(default=72)

    # ----- Job knobs -----
    IG_MAX_POSTS_PER_JOB: int = Field(default=2000)
    IG_COMMENT_DEFAULT_LIMIT: int = Field(default=50)
    IG_DEFAULT_INTERVAL_HOURS: int = Field(default=24)
    IG_TARGET_INTERVAL_JITTER_PCT: int = Field(default=15)

    # ----- Hashtag enrichment / auto-promotion -----
    IG_AUTO_PROMOTE_DISCOVERED: bool = Field(default=False)
    IG_MIN_FOLLOWERS_FOR_ENRICH: int = Field(default=5000)
    IG_MIN_MEDIA_FOR_ENRICH: int = Field(default=12)
    IG_MIN_SCORE_FOR_ENRICH: float = Field(default=50.0)

    # ----- Webhooks (M9) -----
    IG_WEBHOOK_DISPATCH_INTERVAL_SECONDS: int = Field(default=5)
    IG_WEBHOOK_BATCH_SIZE: int = Field(default=25)

    # ----- Retention / GDPR (M9) -----
    IG_RETENTION_ENABLED: bool = Field(default=False)
    IG_COMMENT_TTL_DAYS: int = Field(default=0, description="0 = never expire.")
    IG_BIOGRAPHY_TTL_DAYS: int = Field(default=0)

    # ----- Canary (M10) -----
    IG_CANARY_TARGET: Optional[str] = Field(default=None, description="Username probed hourly with the canary account.")
    IG_CANARY_INTERVAL_HOURS: int = Field(default=1)

    # ----- HikerAPI (third-party Instagram data provider) -----
    # When USE_HIKERAPI=true, scrapers route through HikerAPI's REST
    # service instead of instagrapi. The provider handles login,
    # proxies, and IG anti-bot — we only call HTTP. See docs at
    # https://hiker-doc.readthedocs.io
    USE_HIKERAPI: bool = Field(default=False)
    HIKERAPI_KEY: Optional[str] = Field(default=None, description="x-access-key header value.")
    HIKERAPI_BASE_URL: str = Field(default="https://api.hikerapi.com")
    HIKERAPI_TIMEOUT_SECONDS: float = Field(default=30.0)
    HIKERAPI_MAX_RETRIES: int = Field(default=3)
    HIKERAPI_PAGE_SIZE: int = Field(default=50)
    # HikerAPI charges 2x when `privacy_check=true` (default) because
    # they run an extra query to verify account visibility. We don't
    # need that — our `is_private` field comes from the user payload
    # itself. Default false to halve the billable request count.
    HIKERAPI_PRIVACY_CHECK: bool = Field(default=False)

    # ----- Scoring (deterministic, Phase 1) -----
    IG_SCORE_HALFLIFE_DAYS: float = Field(default=14.0)
    IG_SCORE_W_ENGAGEMENT: float = Field(default=0.20)
    IG_SCORE_W_VELOCITY: float = Field(default=0.25)
    IG_SCORE_W_VIEW_EFFICIENCY: float = Field(default=0.10)
    IG_SCORE_W_COMMENT_INTENSITY: float = Field(default=0.10)
    IG_SCORE_W_AUTHOR_RELATIVE: float = Field(default=0.25)
    IG_SCORE_W_FRESHNESS: float = Field(default=0.10)

    @property
    def allowed_origins(self) -> List[str]:
        """Parsed CORS origins.

        Accepts JSON arrays (`["http://a","http://b"]`) and comma-separated
        strings (`http://a,http://b`). Single `*` short-circuits to `["*"]`.
        """
        raw = self.ALLOWED_ORIGINS.strip()
        if not raw or raw == "*":
            return ["*"]
        if raw.startswith("["):
            import json

            try:
                value = json.loads(raw)
                if isinstance(value, list):
                    return [str(item) for item in value]
            except json.JSONDecodeError:
                pass
        return [s.strip() for s in raw.split(",") if s.strip()]

    @property
    def postgres_dsn(self) -> str:
        """Primary Postgres DSN."""
        return (
            f"postgresql://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    @property
    def postgres_read_dsn(self) -> str:
        """Read DSN — replica if configured, else the primary."""
        return self.POSTGRES_READ_REPLICA_DSN or self.postgres_dsn


settings = Settings()
