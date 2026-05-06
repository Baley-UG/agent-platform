"""Pure-function tests for tracked-target scheduling logic.

DB-side CRUD is exercised in M10 end-to-end. Here we lock in the
job-type selection rules so a refactor doesn't accidentally enqueue
the wrong scraper.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

from cryptography.fernet import Fernet
import pytest


@pytest.fixture(autouse=True)
def _setup_env(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("IG_SECRET_KEY", Fernet.generate_key().decode())
    import importlib

    import app.core.config as cfg

    importlib.reload(cfg)
    import app.services.targets as targets

    importlib.reload(targets)
    return targets


def _target(**kwargs):
    """Build a SimpleNamespace that quacks like ScanTarget for the
    pure-logic helpers (`_job_types_for_target`, `_build_job`)."""
    defaults = dict(
        id=uuid4(),
        kind="user",
        value="anyone",
        status="active",
        interval_hours=24,
        fetch_feed=True,
        fetch_stories=True,
        fetch_highlights=False,
        fetch_comments=True,
        comment_limit=50,
        min_likes=None,
        min_impressions=None,
        hashtag_section="top",
        first_backfill_done=False,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def test_user_first_run_emits_full_plus_stories(_setup_env):
    types = _setup_env._job_types_for_target(_target(first_backfill_done=False))
    assert types == ["user_feed_full", "user_stories"]


def test_user_subsequent_run_emits_incremental(_setup_env):
    types = _setup_env._job_types_for_target(_target(first_backfill_done=True))
    assert types == ["user_feed_incremental", "user_stories"]


def test_user_without_stories(_setup_env):
    types = _setup_env._job_types_for_target(
        _target(first_backfill_done=True, fetch_stories=False)
    )
    assert types == ["user_feed_incremental"]


def test_first_run_with_highlights(_setup_env):
    types = _setup_env._job_types_for_target(
        _target(first_backfill_done=False, fetch_highlights=True)
    )
    assert types == ["user_feed_full", "user_stories", "user_highlights"]


def test_subsequent_run_skips_highlights_by_default(_setup_env):
    """Highlights only enqueue on the first scan; later runs are
    operator-triggered (via run-now) until M10's enrichment polish."""
    types = _setup_env._job_types_for_target(
        _target(first_backfill_done=True, fetch_highlights=True)
    )
    assert "user_highlights" not in types


def test_hashtag_top(_setup_env):
    types = _setup_env._job_types_for_target(
        _target(kind="hashtag", value="moda", hashtag_section="top")
    )
    assert types == ["hashtag_top"]


def test_hashtag_recent(_setup_env):
    types = _setup_env._job_types_for_target(
        _target(kind="hashtag", value="moda", hashtag_section="recent")
    )
    assert types == ["hashtag_recent"]


def test_jitter_keeps_next_run_within_band(_setup_env):
    """next_run_at = now + interval_hours ± IG_TARGET_INTERVAL_JITTER_PCT/2."""
    target = _target(interval_hours=24)
    samples = [_setup_env._bumped_next_run_at(target) for _ in range(50)]

    now = datetime.now(timezone.utc)
    deltas = [(s - now).total_seconds() / 3600 for s in samples]
    # Default jitter pct is 15 → range ~24h ± 15% (~3.6h band each way).
    assert all(20.0 <= d <= 28.0 for d in deltas), deltas
    # Spread should be non-trivial.
    assert max(deltas) - min(deltas) > 0.5


def test_normalise_value_strips_prefix(_setup_env):
    assert _setup_env._normalise_value("user", "@BrandTR") == "brandtr"
    assert _setup_env._normalise_value("hashtag", "#YeniKoleksiyon") == "yenikoleksiyon"
    assert _setup_env._normalise_value("hashtag", "  Sale  ") == "sale"
