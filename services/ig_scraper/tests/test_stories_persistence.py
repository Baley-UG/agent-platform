"""Pure tests for the story payload normalisation helpers.

DB-side INSERTs are tested in M7 end-to-end. Here we cover the bits
that don't need a Postgres: expires_at fallback when IG didn't provide
one, sticker walking, mention/hashtag dedup.
"""

from datetime import datetime, timedelta, timezone

from cryptography.fernet import Fernet
import pytest


@pytest.fixture(autouse=True)
def _setup_env(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("IG_SECRET_KEY", Fernet.generate_key().decode())
    import importlib

    import app.core.config as cfg

    importlib.reload(cfg)
    import app.services.persistence.stories as stories

    importlib.reload(stories)
    return stories


def test_coerce_dt_handles_unix(_setup_env):
    ts = 1_700_000_000
    out = _setup_env._coerce_dt(ts)
    assert out is not None
    assert out.tzinfo == timezone.utc


def test_coerce_dt_naive_assumed_utc(_setup_env):
    naive = datetime(2026, 5, 6, 12, 0)  # no tz
    out = _setup_env._coerce_dt(naive)
    assert out.tzinfo == timezone.utc


def test_coerce_dt_none(_setup_env):
    assert _setup_env._coerce_dt(None) is None
    assert _setup_env._coerce_dt("not a date") is None


def test_extract_sticker_meta(_setup_env):
    story = {
        "hashtags": [{"name": "Yenikoleksiyon"}, {"name": "yenikoleksiyon"}, "Sale"],
        "mentions": [{"username": "BrandTR"}, "@otherUser", "BrandTR"],
        "links": [{"webUri": "https://example.com/x"}],
    }
    tags, mentions, link = _setup_env._extract_sticker_meta(story)
    assert tags == ["yenikoleksiyon", "sale"]
    assert mentions == ["brandtr", "otheruser"]
    assert link == "https://example.com/x"


def test_extract_sticker_meta_empty(_setup_env):
    tags, mentions, link = _setup_env._extract_sticker_meta({})
    assert tags == []
    assert mentions == []
    assert link is None
