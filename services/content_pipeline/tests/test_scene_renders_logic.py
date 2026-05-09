"""Pure logic in scene_renders service — fan-out math + scenario rollup."""

from types import SimpleNamespace

from app.services.scene_renders import _aspect_groups_for, _scene_count, expected_render_count


def _scenario(scenes_count, aspects):
    return SimpleNamespace(
        scenario_json={"scenes": [{"idx": i} for i in range(scenes_count)]},
        target_aspect_groups=aspects,
    )


def test_expected_count_zero_when_no_scenes():
    s = SimpleNamespace(scenario_json=None, target_aspect_groups=["9:16"])
    assert expected_render_count(s) == 0


def test_expected_count_zero_when_no_aspects():
    s = _scenario(scenes_count=4, aspects=[])
    assert expected_render_count(s) == 0


def test_expected_count_multiplies_scenes_and_aspects():
    s = _scenario(scenes_count=5, aspects=["9:16", "4:5"])
    assert expected_render_count(s) == 10


def test_scene_count_ignores_missing_scenario_json():
    s = SimpleNamespace(scenario_json=None, target_aspect_groups=["9:16"])
    assert _scene_count(s) == 0


def test_scene_count_handles_missing_scenes_key():
    s = SimpleNamespace(scenario_json={}, target_aspect_groups=["9:16"])
    assert _scene_count(s) == 0


def test_aspect_groups_normalize_to_list():
    s = _scenario(scenes_count=1, aspects=["9:16"])
    assert _aspect_groups_for(s) == ["9:16"]
    s.target_aspect_groups = None
    assert _aspect_groups_for(s) == []
