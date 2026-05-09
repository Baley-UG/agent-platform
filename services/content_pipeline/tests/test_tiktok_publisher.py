"""TikTok publisher contract tests — construction, header shape."""

import pytest

from app.services.providers.social.tiktok import TikTokPublishError, TikTokPublisher


def test_refuses_empty_token():
    with pytest.raises(TikTokPublishError, match="access_token"):
        TikTokPublisher(access_token="")


def test_constructs_with_token_only():
    p = TikTokPublisher(access_token="t")
    assert p.access_token == "t"
    assert p.open_id == ""


def test_constructs_with_open_id():
    p = TikTokPublisher(access_token="t", open_id="user-1")
    assert p.open_id == "user-1"


def test_headers_include_bearer_token():
    p = TikTokPublisher(access_token="abc123")
    headers = p._headers()
    assert headers["Authorization"] == "Bearer abc123"
    assert headers["Content-Type"].startswith("application/json")
