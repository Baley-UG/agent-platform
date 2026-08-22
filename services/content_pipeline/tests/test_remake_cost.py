"""Cost estimation — the $ figure the operator sees at Gate 1."""

from __future__ import annotations

import uuid

from app.models.remake_shots import RemakeShot
from app.services import remake_cost as cost


def _shot(technique, dur=4.0):
    return RemakeShot(remake_id=uuid.uuid4(), idx=0, start_sec=0.0, end_sec=dur, technique=technique)


def test_copy_and_drop_are_free():
    assert cost.estimate_shot(_shot("copy")) == 0.0
    assert cost.estimate_shot(_shot("drop")) == 0.0


def test_erase_is_flat():
    assert cost.estimate_shot(_shot("erase")) == cost._ERASE_FLAT_USD


def test_restyle_scales_with_duration():
    cheap = cost.estimate_shot(_shot("restyle", dur=2.0))
    dear = cost.estimate_shot(_shot("restyle", dur=8.0))
    assert dear > cheap > 0
    assert abs(cheap - cost._RESTYLE_PER_SEC_USD * 2.0) < 1e-6


def test_reframe_includes_two_keyframes_plus_i2v():
    c = cost.estimate_shot(_shot("reframe", dur=5.0))
    expected = 2 * cost._REFRAME_KEYFRAME_USD + cost._REFRAME_I2V_PER_SEC_USD * 5.0
    assert abs(c - round(expected, 4)) < 1e-6


def test_copy_only_ad_is_basically_free():
    # A fully-copy ad costs only the flat analysis fee.
    shots = [_shot("copy"), _shot("copy"), _shot("copy")]
    total = cost._ANALYSIS_FLAT_USD + sum(cost.estimate_shot(s) for s in shots)
    assert total == cost._ANALYSIS_FLAT_USD


def test_restyle_bills_the_trimmed_window():
    s = _shot("restyle", dur=10.0)
    s.trim_start_sec = 2.0
    s.trim_end_sec = 5.0  # 3s trimmed window, not the full 10s
    assert abs(cost.estimate_shot(s) - cost._RESTYLE_PER_SEC_USD * 3.0) < 1e-6
