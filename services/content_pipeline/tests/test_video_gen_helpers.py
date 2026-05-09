"""Pure helpers in video_gen worker — scene lookup, motion fallback, duration coercion."""

from types import SimpleNamespace

from app.workers.video_gen import _motion_prompt, _scene_duration, _scene_for_idx


def test_scene_for_idx_finds_match():
    scenario = SimpleNamespace(scenario_json={"scenes": [{"idx": 0}, {"idx": 1, "image_prompt": "p"}]})
    assert _scene_for_idx(scenario, 1) == {"idx": 1, "image_prompt": "p"}


def test_scene_for_idx_returns_none_on_missing():
    scenario = SimpleNamespace(scenario_json={"scenes": [{"idx": 0}]})
    assert _scene_for_idx(scenario, 99) is None


def test_scene_for_idx_handles_null_scenario_json():
    scenario = SimpleNamespace(scenario_json=None)
    assert _scene_for_idx(scenario, 0) is None


def test_motion_prompt_uses_field_when_present():
    assert _motion_prompt({"motion_prompt": "slow dolly forward"}) == "slow dolly forward"


def test_motion_prompt_falls_back_to_image_prompt_with_default_motion():
    out = _motion_prompt({"image_prompt": "modern kitchen"})
    assert "modern kitchen" in out
    assert "motion" in out.lower()


def test_motion_prompt_handles_completely_empty_scene():
    out = _motion_prompt({})
    assert out  # non-empty default
    assert "motion" in out.lower()


def test_scene_duration_uses_field_when_positive():
    assert _scene_duration({"duration": 7}) == 7.0


def test_scene_duration_falls_back_when_missing_or_invalid():
    assert _scene_duration({}, fallback=4.0) == 4.0
    assert _scene_duration({"duration": 0}, fallback=4.0) == 4.0
    assert _scene_duration({"duration": -3}, fallback=4.0) == 4.0
