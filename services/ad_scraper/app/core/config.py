"""Application configuration — single source of truth for tuning knobs.

This file is the catalogue. Every `Field(default=...)` below is the
production-ready value; .env only needs to override when a particular
deployment differs from the default. Secrets (`AD_SCRAPER_API_KEY`,
`AD_SECRET_KEY`) and host-specific stuff (`POSTGRES_*`, `S3_*`) live in
.env. Tuning (timeouts, retries, page limits, mirror policy) lives HERE
with explanations next to each knob.

Reuses the same POSTGRES_* / S3_* keys as the main agent-platform,
ig_scraper and content_pipeline so all four can talk to the same
database and bucket without duplicating credentials.
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
    - **Dev checkout**: `…/agent-platform/services/ad_scraper/app/core/config.py`
      → `parents[2]` is the service dir, `parents[4]` is the repo root.
    - **Container** (`/app/app/core/config.py`): `parents[2]` is `/app`,
      no repo root available. docker-compose's `env_file:` directive
      already injected env vars before this code runs, so missing files
      here are fine.
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
    """ad_scraper service settings."""

    model_config = SettingsConfigDict(env_file=None, case_sensitive=True, extra="ignore")

    # ----- Service identity (constants, NOT read from env to avoid clashing
    # with PROJECT_NAME/VERSION env vars used by the main agent-platform app) -----
    PROJECT_NAME: ClassVar[str] = "ad-scraper"
    VERSION: ClassVar[str] = "0.1.0"
    DESCRIPTION: ClassVar[str] = "AppGrowing/YouCloud ad-intelligence ingestion microservice."
    API_V1_STR: ClassVar[str] = "/api/v1"

    ENVIRONMENT: Environment = Field(default=Environment.DEVELOPMENT, validation_alias="APP_ENV")

    # ----- Auth -----
    AD_SCRAPER_API_KEY: str = Field(default="changeme-not-a-real-key")
    AD_SECRET_KEY: str = Field(
        default="changeme-fernet-key",
        description="Fernet key for encrypting the YouCloud password and session cookie. "
        "Generate with `python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'`.",
    )

    # ----- CORS -----
    # Stored as a raw comma-separated (or JSON) string so pydantic-settings
    # doesn't try to JSON-decode it at the source layer. Use the
    # `allowed_origins` property for the parsed list.
    ALLOWED_ORIGINS: str = Field(default="*")

    # ----- Postgres (shared with agent-platform / ig_scraper / content_pipeline) -----
    POSTGRES_HOST: str = Field(default="db")
    POSTGRES_PORT: int = Field(default=5432)
    POSTGRES_USER: str = Field(default="postgres")
    POSTGRES_PASSWORD: str = Field(default="postgres")
    POSTGRES_DB: str = Field(default="agent_platform")
    POSTGRES_POOL_SIZE: int = Field(default=10)
    POSTGRES_MAX_OVERFLOW: int = Field(default=20)

    # Optional read-replica DSN for BI / analytical queries. If unset,
    # reads fall back to the primary connection.
    POSTGRES_READ_REPLICA_DSN: Optional[str] = Field(default=None)

    # ----- YouCloud / AppGrowing API -----
    AD_API_URL: str = Field(default="https://api-appgrowing-global.youcloud.com/graphql")
    # `origin` and `referer` are validated server-side — a mismatched pair
    # gets rejected, so they track the public web app, not our service.
    AD_API_ORIGIN: str = Field(default="https://appgrowing-global.youcloud.com")
    AD_API_REFERER: str = Field(default="https://appgrowing-global.youcloud.com/leaflet")
    AD_API_USER_AGENT: str = Field(
        default=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
        )
    )
    # The API returns HTTP 406 with a plain-text body when this is absent.
    # It is NOT optional.
    AD_API_LANGUAGE: str = Field(default="en")
    AD_API_TIMEOUT_SECONDS: float = Field(default=45.0)
    # Retries apply to transport errors and the "system is busy" transient
    # error only. Auth / plan / filter errors are terminal by design.
    AD_API_MAX_RETRIES: int = Field(default=3)
    # ----- Rate limiting (see app/services/youcloud/throttle.py) -----
    # Minimum seconds between any two upstream requests, PROCESS-WIDE — not
    # per job. This used to be a per-job page delay, which meant
    # AD_WORKER_CONCURRENCY=2 quietly doubled the rate the endpoint saw.
    AD_API_MIN_REQUEST_INTERVAL_SECONDS: float = Field(default=1.5)
    # Ceiling the interval can grow to after repeated `00:400998` refusals.
    AD_API_MAX_REQUEST_INTERVAL_SECONDS: float = Field(default=20.0)
    # How long the whole process pauses on a rate limit. Generous on
    # purpose: a rate limiter answers patience, and re-running a job from
    # page 1 costs far more requests than waiting does.
    AD_API_RATE_LIMIT_COOLDOWN_SECONDS: float = Field(default=30.0)
    # Rate limits get their own, larger budget than transport errors: it is
    # the one failure where trying again later reliably works.
    AD_API_RATE_LIMIT_MAX_RETRIES: int = Field(default=5)
    # Added, never subtracted. Two worker containers share no throttle
    # state, so without jitter their requests drift into lockstep.
    AD_API_JITTER_RATIO: float = Field(default=0.25)

    # ----- Pagination ceilings (verified against the live API) -----
    # `materialList` refuses `page > 200` with "Parameter error, please
    # clear the filter and refresh", and `limit` is fixed at 50 by the
    # server. That is a hard 10 000-row ceiling PER FILTER SET — to go
    # deeper you must partition the filter space (date window, area,
    # media, platform, keyword), not ask for more pages.
    AD_MAX_PAGE: int = Field(default=200)
    AD_PAGE_SIZE: int = Field(default=50)
    AD_DEFAULT_PAGE_TO: int = Field(default=5, description="Default last page when a job omits page_to.")

    # ----- Worker -----
    AD_WORKER_CONCURRENCY: int = Field(default=2)
    # The worker does all the ingestion, so every counter that matters
    # (pages fetched, rate limits, throttle waits, mirror bytes) lives in
    # ITS process — and the API's /metrics only ever reports its own. Without
    # an exporter here those counters read 0 forever, which is worse than
    # having no metric at all. 0 disables it.
    AD_WORKER_METRICS_PORT: int = Field(default=9103)
    AD_WORKER_POLL_SECONDS: float = Field(default=3.0)
    # A job whose worker died mid-process stays stuck in `status='running'`
    # (the worker only claims `queued`). Requeued by the API's
    # `POST /jobs/{id}/retry` or by a future reaper.
    AD_JOB_STUCK_AFTER_MINUTES: int = Field(default=60)
    AD_JOB_MAX_ATTEMPTS: int = Field(default=3)

    # ----- Session / credentials -----
    # Refresh the session this long before the JWT `exp` claim so a job
    # never starts on a cookie that dies mid-run.
    AD_SESSION_REFRESH_MARGIN_SECONDS: int = Field(default=1800)
    # After this many consecutive login failures the credential flips to
    # `login_failed` and jobs soft-fail until an operator pastes a fresh
    # cookie via `PUT /api/v1/credentials/session`.
    AD_LOGIN_MAX_CONSECUTIVE_FAILURES: int = Field(default=3)

    # ----- S3 mirror for scraped creatives -----
    # YouCloud media URLs are signed with `auth_key=<epoch>-...` and die
    # roughly 15 days out, so mirroring is the whole point of this
    # service — hence `always` by default (unlike ig_scraper's `auto`,
    # which keys off tracked authors; there is no such notion here).
    #   always → mirror every persisted material
    #   job    → mirror only when the job was created with `mirror=true`
    #   never  → disable entirely (metadata-only ingestion)
    AD_MIRROR_MEDIA: str = Field(default="always", description="always | job | never")
    AD_MIRROR_MAX_BYTES: int = Field(default=80 * 1024 * 1024, description="Ad videos run long; 80 MB headroom.")
    AD_MIRROR_TIMEOUT_SECONDS: float = Field(default=60.0)

    # Shared bucket with ig_scraper + content_pipeline. ad_scraper writes
    # under the prefix `ad-scraper/materials/<material_id>/...`.
    S3_ENDPOINT: Optional[str] = Field(default=None)
    S3_BUCKET: Optional[str] = Field(default=None)
    S3_ACCESS_KEY: Optional[str] = Field(default=None)
    S3_SECRET_KEY: Optional[str] = Field(default=None)
    S3_REGION: str = Field(default="us-east-1")
    S3_USE_PATH_STYLE: bool = Field(default=True)
    S3_PRESIGNED_URL_TTL_SECONDS: int = Field(default=3600)

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

    @property
    def max_rows_per_filter_set(self) -> int:
        """Hard ceiling on rows any single filter set can ever yield."""
        return self.AD_MAX_PAGE * self.AD_PAGE_SIZE


settings = Settings()
