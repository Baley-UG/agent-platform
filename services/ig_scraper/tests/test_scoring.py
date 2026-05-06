"""Pure-function tests for the score formula.

The DB-side wrappers (`update_post_score`, `recompute_recent_batch`,
`refresh_views`) are exercised end-to-end in M10 with a real Postgres.
M8 tests focus on the math.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from cryptography.fernet import Fernet
import pytest


@pytest.fixture(autouse=True)
def _setup_env(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("IG_SECRET_KEY", Fernet.generate_key().decode())
    # Pin every weight + halflife so the asserts below are stable.
    monkeypatch.setenv("IG_SCORE_HALFLIFE_DAYS", "14")
    monkeypatch.setenv("IG_SCORE_W_ENGAGEMENT", "0.20")
    monkeypatch.setenv("IG_SCORE_W_VELOCITY", "0.25")
    monkeypatch.setenv("IG_SCORE_W_VIEW_EFFICIENCY", "0.10")
    monkeypatch.setenv("IG_SCORE_W_COMMENT_INTENSITY", "0.10")
    monkeypatch.setenv("IG_SCORE_W_AUTHOR_RELATIVE", "0.25")
    monkeypatch.setenv("IG_SCORE_W_FRESHNESS", "0.10")
    import importlib

    import app.core.config as cfg

    importlib.reload(cfg)
    import app.services.scoring as scoring

    importlib.reload(scoring)
    return scoring


def _now():
    return datetime.now(timezone.utc)


def test_zero_engagement_zero_score(_setup_env):
    """A post with no engagement and computed today still gets a small
    boost from freshness (≈10% × 1.0 = 10) but nothing else."""
    r = _setup_env.compute_score(
        like_count=0, comment_count=0, play_count=None, view_count=None,
        follower_count=10_000, taken_at=_now(),
        velocity_likes_per_hour=0.0, author_relative=0.5,
    )
    # freshness = 1.0 → 10 points; author_relative=0.5 → 12.5 points = 22.5
    assert 20 <= r.score <= 25, r.score


def test_perfect_inputs_max_out(_setup_env):
    """All components saturated → score should be ~100."""
    r = _setup_env.compute_score(
        like_count=10_000, comment_count=10_000, play_count=200_000, view_count=None,
        follower_count=1_000, taken_at=_now(),  # impossibly high engagement_rate
        velocity_likes_per_hour=10_000.0, author_relative=1.0,
    )
    assert r.score >= Decimal("99")


def test_freshness_decays(_setup_env):
    new = _setup_env.compute_score(
        like_count=100, comment_count=10, play_count=None, view_count=None,
        follower_count=10_000, taken_at=_now(),
        velocity_likes_per_hour=10.0, author_relative=0.5,
    )
    old = _setup_env.compute_score(
        like_count=100, comment_count=10, play_count=None, view_count=None,
        follower_count=10_000, taken_at=_now() - timedelta(days=60),
        velocity_likes_per_hour=10.0, author_relative=0.5,
    )
    assert old.score < new.score


def test_view_efficiency_only_for_video(_setup_env):
    photo = _setup_env.compute_score(
        like_count=500, comment_count=20, play_count=None, view_count=None,
        follower_count=10_000, taken_at=_now(),
        velocity_likes_per_hour=10.0, author_relative=0.5,
    )
    # Same engagement but with play_count → view_efficiency contributes.
    video = _setup_env.compute_score(
        like_count=500, comment_count=20, play_count=2_000, view_count=None,
        follower_count=10_000, taken_at=_now(),
        velocity_likes_per_hour=10.0, author_relative=0.5,
    )
    assert video.score > photo.score
    assert photo.components["view_efficiency"] == 0.0
    assert video.components["view_efficiency"] > 0


def test_score_is_clipped_to_0_100(_setup_env):
    """Even with absurd inputs the final score never escapes [0,100]."""
    r = _setup_env.compute_score(
        like_count=1_000_000_000, comment_count=1_000_000_000,
        play_count=1_000_000_000, view_count=None,
        follower_count=1, taken_at=_now(),
        velocity_likes_per_hour=1_000_000.0, author_relative=1.0,
    )
    assert Decimal(0) <= r.score <= Decimal(100)


def test_components_preserved(_setup_env):
    r = _setup_env.compute_score(
        like_count=100, comment_count=10, play_count=1_000, view_count=None,
        follower_count=10_000, taken_at=_now(),
        velocity_likes_per_hour=5.0, author_relative=0.7,
    )
    assert set(r.components) == {
        "engagement_rate",
        "velocity",
        "view_efficiency",
        "comment_intensity",
        "author_relative",
        "freshness",
    }
    # Each component should land in [0,1].
    for name, value in r.components.items():
        assert 0.0 <= value <= 1.0, f"{name}={value}"


def test_velocity_normalised(_setup_env):
    """100 likes/hour saturates the velocity component at 1.0."""
    r = _setup_env.compute_score(
        like_count=0, comment_count=0, play_count=None, view_count=None,
        follower_count=10_000, taken_at=_now(),
        velocity_likes_per_hour=200.0, author_relative=0.5,
    )
    assert r.components["velocity"] == 1.0
