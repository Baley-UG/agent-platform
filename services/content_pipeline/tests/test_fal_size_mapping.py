"""fal.ai dimension → image_size string mapping."""

from app.services.providers.image.fal import _fal_size


def test_canonical_9_16_maps_to_preset_name():
    assert _fal_size(1080, 1920) == {"image_size": "portrait_16_9"}


def test_square_1080_maps_to_preset():
    assert _fal_size(1080, 1080) == {"image_size": "square"}


def test_4_5_maps_to_preset():
    assert _fal_size(1080, 1350) == {"image_size": "portrait_4_3"}


def test_landscape_16_9_maps_to_preset():
    assert _fal_size(1920, 1080) == {"image_size": "landscape_16_9"}


def test_non_canonical_falls_back_to_explicit_dims():
    assert _fal_size(1280, 720) == {"image_size": {"width": 1280, "height": 720}}
