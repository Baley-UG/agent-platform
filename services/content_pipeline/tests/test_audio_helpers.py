"""audio.build_voiceover_script + select_music_for_scenario logic."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from app.services.audio import build_voiceover_script, select_music_for_scenario


def _scenario(scenes=None, music_mood=None, project_id="00000000-0000-0000-0000-000000000000"):
    return SimpleNamespace(
        project_id=project_id,
        scenario_json={
            "scenes": scenes or [],
            "music": {"mood": music_mood} if music_mood is not None else {},
        },
    )


def test_voiceover_script_concatenates_scenes_with_periods():
    s = _scenario(scenes=[{"voiceover": "first line"}, {"voiceover": "second line!"}])
    out = build_voiceover_script(s)
    assert "first line." in out
    assert "second line!." not in out  # we strip trailing ! before adding .
    assert "second line." in out


def test_voiceover_script_handles_empty_scenes_with_pause():
    s = _scenario(scenes=[{"voiceover": "intro"}, {"voiceover": ""}, {"voiceover": "outro"}])
    out = build_voiceover_script(s)
    assert "..." in out
    assert "intro." in out
    assert "outro." in out


def test_voiceover_script_returns_empty_when_no_scenario_json():
    s = SimpleNamespace(project_id="x", scenario_json=None)
    assert build_voiceover_script(s) == ""


def test_select_music_prefers_mood_overlap():
    track_a = SimpleNamespace(id="a", project_id="p", mood=["chill"], created_at=1)
    track_b = SimpleNamespace(id="b", project_id="p", mood=["uplifting_lofi"], created_at=2)
    session = MagicMock()
    session.exec.return_value.all.return_value = [track_a, track_b]

    s = _scenario(music_mood=["uplifting_lofi"])
    selected = select_music_for_scenario(session, s)
    assert selected is track_b


def test_select_music_falls_back_to_newest_when_no_overlap():
    track_a = SimpleNamespace(id="a", project_id="p", mood=["chill"], created_at=2)
    track_b = SimpleNamespace(id="b", project_id="p", mood=["epic"], created_at=1)
    session = MagicMock()
    session.exec.return_value.all.return_value = [track_a, track_b]  # newest first

    s = _scenario(music_mood=["uplifting_lofi"])
    selected = select_music_for_scenario(session, s)
    assert selected is track_a


def test_select_music_returns_none_for_empty_library():
    session = MagicMock()
    session.exec.return_value.all.return_value = []
    s = _scenario(music_mood=["any"])
    assert select_music_for_scenario(session, s) is None


def test_select_music_handles_string_mood_block():
    track_a = SimpleNamespace(id="a", project_id="p", mood=["epic"], created_at=2)
    track_b = SimpleNamespace(id="b", project_id="p", mood=["chill"], created_at=1)
    session = MagicMock()
    session.exec.return_value.all.return_value = [track_a, track_b]
    s = _scenario(music_mood="chill")
    assert select_music_for_scenario(session, s) is track_b
