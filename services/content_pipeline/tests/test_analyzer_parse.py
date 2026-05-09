"""Analyzer JSON parsing + validation."""

import json

import pytest

from app.services.analyzer import build_user_prompt, parse_scenario_json, validate_scenario


def _scenario(scenes=None, duration=18):
    return {
        "duration_sec": duration,
        "hook": "first 2s grabber",
        "cta": "follow",
        "music": {"mood": "uplifting_lofi", "bpm_range": [80, 100]},
        "outro_template_id": None,
        "scenes": scenes
        or [
            {"idx": 0, "duration": 6, "image_prompt": "p", "motion_prompt": "m", "on_screen_text": "", "transition_out": "cut", "voiceover": "", "audio_mood": "calm"},
            {"idx": 1, "duration": 6, "image_prompt": "p", "motion_prompt": "m", "on_screen_text": "", "transition_out": "cut", "voiceover": "", "audio_mood": "calm"},
            {"idx": 2, "duration": 6, "image_prompt": "p", "motion_prompt": "m", "on_screen_text": "", "transition_out": "cut", "voiceover": "", "audio_mood": "calm"},
        ],
    }


def test_parse_plain_json():
    text = json.dumps(_scenario())
    parsed = parse_scenario_json(text)
    assert parsed["duration_sec"] == 18


def test_parse_strips_fenced_block():
    text = "```json\n" + json.dumps(_scenario()) + "\n```"
    parsed = parse_scenario_json(text)
    assert "scenes" in parsed


def test_parse_extracts_outermost_object_when_prefixed_with_chatter():
    chatter = "Sure, here's the scenario:\n" + json.dumps(_scenario()) + "\nLet me know if you want changes."
    parsed = parse_scenario_json(chatter)
    assert parsed["duration_sec"] == 18


def test_validate_rejects_empty_scenes():
    bad = _scenario()
    bad["scenes"] = []
    with pytest.raises(ValueError, match="non-empty"):
        validate_scenario(bad)


def test_validate_rejects_missing_scene_fields():
    bad = _scenario(scenes=[{"idx": 0}])
    with pytest.raises(ValueError, match="missing required field"):
        validate_scenario(bad)


def test_build_user_prompt_includes_caption_transcript_metadata():
    class FakeRef:
        source_provider = "instagram"
        source_url = "https://instagram.com/p/abc"
        caption = "morning routine ☕"
        transcript = "every morning I'd do the same thing"
        hashtags = ["#morning", "#coffee"]
        metadata_json = {"media_type": "video", "play_count": 12345, "score": 78.5}

    prompt = build_user_prompt(FakeRef(), brand_style_suffix="cinematic warm")
    assert "morning routine" in prompt
    assert "every morning" in prompt
    assert "play_count: 12345" in prompt
    assert "cinematic warm" in prompt
    assert "Originality is mandatory" in prompt
