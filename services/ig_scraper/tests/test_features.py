"""Caption feature extraction tests.

Covers:
- Hashtag/mention extraction (incl. case-insensitivity and Turkish chars)
- has_question / has_cta detection in TR + EN
- Empty / None caption handling
- Simhash determinism + Hamming distance ordering
"""

from cryptography.fernet import Fernet
import pytest


@pytest.fixture(autouse=True)
def _setup_env(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("IG_SECRET_KEY", Fernet.generate_key().decode())
    import importlib

    import app.core.config as cfg

    importlib.reload(cfg)
    import app.services.features as features

    importlib.reload(features)
    return features


def test_empty_caption(_setup_env):
    f = _setup_env.extract(None)
    assert f.caption_length == 0
    assert f.hashtags == []
    assert f.mentions == []
    assert f.has_question is False
    assert f.has_cta is False
    assert f.caption_simhash_signed == 0


def test_english_caption(_setup_env):
    f = _setup_env.extract("Check out our new drop! #fashion @brand 🔥 link in bio")
    assert "fashion" in f.hashtags
    assert "brand" in f.mentions
    assert f.emoji_count == 1
    assert f.hashtag_count == 1
    assert f.mention_count == 1
    assert f.has_cta is True
    assert f.has_question is False


def test_turkish_caption(_setup_env):
    text = "Yeni koleksiyon geldi! Sen de almak ister misin? Linkler biyodaki #yenikoleksiyon"
    f = _setup_env.extract(text)
    assert "yenikoleksiyon" in f.hashtags
    assert f.has_question is True   # has '?'
    assert f.has_cta is True        # 'biyodaki'
    # Caption length matches the input length.
    assert f.caption_length == len(text)


def test_question_via_turkish_particle(_setup_env):
    f = _setup_env.extract("Bu ürün size uygun mu kardeşim")
    assert f.has_question is True


def test_hashtag_dedup_preserves_order(_setup_env):
    f = _setup_env.extract("#a #b #a #c")
    assert f.hashtags == ["a", "b", "c"]


def test_simhash_determinism(_setup_env):
    a = _setup_env.extract("yeni gelen ürünler harika").caption_simhash_signed
    b = _setup_env.extract("yeni gelen ürünler harika").caption_simhash_signed
    assert a == b


def test_simhash_similarity(_setup_env):
    """Near-duplicates should have lower Hamming distance than unrelated text."""
    from app.services.simhash import from_signed_64, hamming_distance

    base = _setup_env.extract("yeni koleksiyon harika ve şık").caption_simhash_signed
    near = _setup_env.extract("yeni koleksiyon harika").caption_simhash_signed
    far = _setup_env.extract(
        "Pazartesi günü kahve içerken kediyi seyrettim sahil çok güzeldi"
    ).caption_simhash_signed

    d_near = hamming_distance(from_signed_64(base), from_signed_64(near))
    d_far = hamming_distance(from_signed_64(base), from_signed_64(far))
    assert d_near < d_far, f"near={d_near} far={d_far}"


def test_signed_round_trip(_setup_env):
    """signed→unsigned→signed is identity for any 64-bit value."""
    from app.services.simhash import from_signed_64, to_signed_64

    for value in [0, 1, (1 << 63) - 1, 1 << 63, (1 << 64) - 1]:
        assert from_signed_64(to_signed_64(value)) == value
