"""HikerAPI-backed scrapers.

Drop-in replacement for the instagrapi scrapers when `USE_HIKERAPI=true`.
The HikerAPI service handles login / proxies / IG anti-bot internally
— we just send authenticated HTTP requests and shape the responses
into the same dicts our persistence layer already understands.

Why this is a separate package:
  * Mevcut instagrapi-based scraper'larla yan yana yaşar — tek satır
    config (`USE_HIKERAPI`) kontrolü ile geçiş yapılır.
  * No instagrapi imports here, no proxy/account dependencies.
  * Worker, hikerapi scraper'larını `requires_account=False` ile register
    ederek account_pool.acquire'i bypass eder.
"""

from app.services.scrapers import register
from app.services.scrapers.hikerapi.hashtag import (
    run_hashtag_recent_hk,
    run_hashtag_top_hk,
)
from app.services.scrapers.hikerapi.user_feed import (
    run_user_feed_full_hk,
    run_user_feed_incremental_hk,
)
from app.services.scrapers.hikerapi.user_stories import run_user_stories_hk


def register_all() -> None:
    """Override the instagrapi scrapers with HikerAPI versions.

    Called from `app/services/scrapers/__init__.py` when
    `settings.USE_HIKERAPI` is true. After this, the dispatcher routes
    every supported job_type to HikerAPI; instagrapi scrapers stay
    importable but unused.
    """
    register(
        "user_feed_full",
        run_user_feed_full_hk,
        requires_account=False,
    )
    register(
        "user_feed_incremental",
        run_user_feed_incremental_hk,
        requires_account=False,
    )
    register(
        "user_stories",
        run_user_stories_hk,
        requires_account=False,
    )
    register(
        "hashtag_top",
        run_hashtag_top_hk,
        requires_account=False,
    )
    register(
        "hashtag_recent",
        run_hashtag_recent_hk,
        requires_account=False,
    )
