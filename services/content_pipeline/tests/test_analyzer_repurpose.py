"""Repurpose-mode analyzer: prompt shape + validation branch.

The key regression guard here is that `mode="recreate"` behaviour is
untouched — every existing scenario must keep validating exactly as it
did before repurpose existed.
"""

from __future__ import annotations

import pytest

from app.services import analyzer as svc
from app.services.segments import Segment


class FakeReference:
    def __init__(self):
        self.id = "ref-1"
        self.source_provider = "instagram"
        self.source_url = "https://instagram.com/p/abc"
        self.caption = "before and after"
        self.transcript = None
        self.hashtags = ["ai", "photo"]
        self.metadata_json = {"media_type": 2, "product_type": "clips", "username": "rival"}


def _segments():
    return [
        Segment(idx=1, start_sec=0.0, end_sec=2.4, frame_s3_key="f0.jpg"),
        Segment(idx=2, start_sec=2.4, end_sec=4.85, frame_s3_key="f1.jpg"),
    ]


def _scene(idx, **over):
    base = {
        "idx": idx,
        "segment_idx": idx,
        "action": "keep",
        "duration": 2.4,
        "on_screen_text": "hi",
    }
    base.update(over)
    return base


def _payload(*scenes):
    return {"scenes": list(scenes)}


# ---------------------------------------------------------------------------
# prompt
# ---------------------------------------------------------------------------


def test_prompt_lists_every_segment_with_its_window():
    prompt = svc.build_repurpose_user_prompt(FakeReference(), _segments())
    assert "SEGMENT 1  0.00–2.40s" in prompt
    assert "SEGMENT 2  2.40–4.85s" in prompt


def test_prompt_states_the_image_alignment():
    prompt = svc.build_repurpose_user_prompt(FakeReference(), _segments())
    assert "image N attached below = segment N" in prompt


def test_prompt_drops_the_recreate_closing_directive():
    prompt = svc.build_repurpose_user_prompt(FakeReference(), _segments())
    # The recreate directive tells the model to invent original frames —
    # exactly wrong here.
    assert "do NOT recreate specific frames" not in prompt


def test_prompt_keeps_the_reference_brief():
    prompt = svc.build_repurpose_user_prompt(FakeReference(), _segments())
    assert "before and after" in prompt  # caption survives
    assert "@rival" in prompt


def test_prompt_carries_brand_voice():
    prompt = svc.build_repurpose_user_prompt(
        FakeReference(), _segments(), brand_style_suffix="playful, short sentences"
    )
    assert "playful, short sentences" in prompt


def test_system_prompt_defaults_to_keeping_source_audio():
    assert '"source_audio_mode": "keep"' in svc.REPURPOSE_SYSTEM_PROMPT
    assert '"voiceover_enabled": false' in svc.REPURPOSE_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# validation — repurpose
# ---------------------------------------------------------------------------


def test_valid_repurpose_payload_passes():
    payload = _payload(_scene(1), _scene(2))
    assert svc.validate_scenario(payload, mode="repurpose") is payload


def test_repurpose_does_not_require_image_prompts():
    """The whole point: no frames are synthesized, so demanding an
    image_prompt would reject every valid repurpose scenario."""
    svc.validate_scenario(_payload(_scene(1)), mode="repurpose")


def test_missing_segment_idx_is_rejected():
    scene = _scene(1)
    del scene["segment_idx"]
    with pytest.raises(ValueError, match="segment_idx"):
        svc.validate_scenario(_payload(scene), mode="repurpose")


def test_unknown_action_is_rejected():
    with pytest.raises(ValueError, match="action"):
        svc.validate_scenario(_payload(_scene(1, action="remix")), mode="repurpose")


def test_replace_without_prompt_is_rejected():
    with pytest.raises(ValueError, match="replace_prompt"):
        svc.validate_scenario(
            _payload(_scene(1, action="replace")), mode="repurpose"
        )


def test_replace_with_prompt_passes():
    svc.validate_scenario(
        _payload(_scene(1, action="replace", replace_prompt="a phone on a desk")),
        mode="repurpose",
    )


def test_blank_replace_prompt_is_rejected():
    with pytest.raises(ValueError, match="replace_prompt"):
        svc.validate_scenario(
            _payload(_scene(1, action="replace", replace_prompt="   ")),
            mode="repurpose",
        )


def test_drop_needs_no_prompt():
    svc.validate_scenario(_payload(_scene(1, action="drop")), mode="repurpose")


def test_scenes_must_cover_every_planned_segment():
    with pytest.raises(ValueError, match="missing=\\[2\\]"):
        svc.validate_scenario(
            _payload(_scene(1)), mode="repurpose", expected_segment_indices={1, 2}
        )


def test_invented_segments_are_rejected():
    with pytest.raises(ValueError, match="unexpected=\\[9\\]"):
        svc.validate_scenario(
            _payload(_scene(1), _scene(9)),
            mode="repurpose",
            expected_segment_indices={1},
        )


def test_exact_coverage_passes():
    svc.validate_scenario(
        _payload(_scene(1), _scene(2)),
        mode="repurpose",
        expected_segment_indices={1, 2},
    )


def test_non_integer_segment_idx_is_rejected():
    with pytest.raises(ValueError, match="not an integer"):
        svc.validate_scenario(
            _payload(_scene(1, segment_idx="first")), mode="repurpose"
        )


# ---------------------------------------------------------------------------
# regression — recreate mode is untouched
# ---------------------------------------------------------------------------


def _recreate_scene():
    return {
        "idx": 1,
        "duration": 3.0,
        "image_prompt": "a delta prompt",
        "motion_prompt": "slow push in",
    }


def test_recreate_still_requires_synthesis_prompts():
    scene = _recreate_scene()
    del scene["image_prompt"]
    with pytest.raises(ValueError, match="image_prompt"):
        svc.validate_scenario(_payload(scene))


def test_recreate_default_mode_unchanged():
    payload = _payload(_recreate_scene())
    assert svc.validate_scenario(payload) is payload


def test_recreate_ignores_repurpose_fields():
    payload = _payload({**_recreate_scene(), "action": "nonsense"})
    svc.validate_scenario(payload, mode="recreate")


def test_empty_scenes_rejected_in_both_modes():
    for mode in ("recreate", "repurpose"):
        with pytest.raises(ValueError, match="non-empty"):
            svc.validate_scenario({"scenes": []}, mode=mode)
