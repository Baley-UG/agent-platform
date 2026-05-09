"""Sanity tests for the Instagram publisher init + variant→media_type mapping.

We don't hit Graph in tests; the actual HTTP flow is exercised in prod.
These tests pin the contract so future refactors can't silently break it.
"""

import pytest

from app.services.providers.social.instagram import (
    InstagramPublishError,
    InstagramPublisher,
    variant_to_ig_media_type,
)


def test_publisher_refuses_empty_token():
    with pytest.raises(InstagramPublishError, match="access_token"):
        InstagramPublisher(access_token="", ig_user_id="123")


def test_publisher_refuses_empty_user_id():
    with pytest.raises(InstagramPublishError, match="ig_user_id"):
        InstagramPublisher(access_token="t", ig_user_id="")


def test_publisher_constructs_with_valid_args():
    p = InstagramPublisher(access_token="t", ig_user_id="123")
    assert p.access_token == "t"
    assert p.ig_user_id == "123"


def test_variant_mapping_reels():
    assert variant_to_ig_media_type("ig_reels") == "REELS"


def test_variant_mapping_story():
    assert variant_to_ig_media_type("ig_story") == "STORIES"


def test_variant_mapping_feed_falls_back_to_video():
    assert variant_to_ig_media_type("ig_feed_45") == "VIDEO"


def test_variant_mapping_unknown_falls_back_to_video():
    assert variant_to_ig_media_type("totally_unknown") == "VIDEO"
