"""Platform preset constants — keep aspect/dimension contract pinned."""

from app.services.presets import ASPECT_DIMENSIONS, PRESETS, aspect_dimensions, variant_aspect


def test_every_variant_has_an_aspect_in_dimensions():
    for key, preset in PRESETS.items():
        assert preset.aspect in ASPECT_DIMENSIONS, f"variant {key} has aspect {preset.aspect} with no dimensions"


def test_9_16_family_shares_master():
    """ig_reels, tiktok, ig_story, yt_shorts all share 9:16 → one master per scene serves all four."""
    nine_sixteens = {key for key, p in PRESETS.items() if p.aspect == "9:16"}
    assert {"ig_reels", "tiktok", "ig_story", "yt_shorts"}.issubset(nine_sixteens)


def test_aspect_dimensions_are_canonical_resolutions():
    assert aspect_dimensions("9:16") == (1080, 1920)
    assert aspect_dimensions("4:5") == (1080, 1350)
    assert aspect_dimensions("1:1") == (1080, 1080)


def test_variant_aspect_lookup():
    assert variant_aspect("ig_reels") == "9:16"
    assert variant_aspect("ig_feed_45") == "4:5"
    assert variant_aspect("ig_feed_11") == "1:1"
    assert variant_aspect("totally_unknown_platform") is None
