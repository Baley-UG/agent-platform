"""Caption resolver — slot override → scenario default → empty."""

from types import SimpleNamespace

from app.services.captions import _coerce_hashtags, resolve


def _slot(caption=None, hashtags=None):
    return SimpleNamespace(caption_override=caption, hashtags_override=hashtags)


def _scenario(default_caption=None, default_hashtags=None):
    return SimpleNamespace(default_caption=default_caption, default_hashtags=default_hashtags)


def test_slot_override_wins():
    out = resolve(_slot("slot text"), _scenario("scenario fallback"))
    assert out == "slot text"


def test_scenario_fallback_used_when_slot_empty():
    out = resolve(_slot(None), _scenario("scenario fallback"))
    assert out == "scenario fallback"


def test_returns_empty_string_when_both_empty():
    assert resolve(_slot(), _scenario()) == ""


def test_hashtags_appended_below_caption():
    out = resolve(_slot("yum", ["food", "morning"]), _scenario())
    assert "yum" in out
    assert "#food" in out
    assert "#morning" in out
    assert out.count("\n\n") == 1


def test_hashtags_only_when_no_caption():
    out = resolve(_slot(None, ["food"]), _scenario(None, ["fallback"]))
    # slot hashtags win even when caption is None
    assert out.strip() == "#food"


def test_scenario_hashtags_used_when_slot_doesnt_provide_them():
    out = resolve(_slot("hi"), _scenario(None, ["x", "y"]))
    assert out.startswith("hi")
    assert "#x" in out and "#y" in out


def test_coerce_hashtags_strips_leading_hash_and_dedups():
    assert _coerce_hashtags(["#food", "food", "FOOD", "morning"]) == ["#food", "#morning"]


def test_coerce_hashtags_handles_empty_and_whitespace():
    assert _coerce_hashtags([" ", "", None, "ok"]) == ["#ok"]
