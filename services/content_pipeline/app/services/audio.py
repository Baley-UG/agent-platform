"""Audio orchestration — voiceover script assembly + music selection.

Voiceover strategy (CP-M5):
- We synthesize the WHOLE scenario as one TTS call. The text is built
  from `scenario.scenario_json.scenes[*].voiceover` joined with
  punctuation gaps proportional to `scene.duration`. The compose stage
  then plays this single audio track over the concatenated scene videos.
  Per-scene voiceovers and tight per-scene timing land in CP-M5.5.

Music strategy (CP-M5):
- Pick one track from the project's `music_tracks` library whose `mood`
  array overlaps with `scenario.scenario_json.music.mood`. If no overlap,
  fall back to any track (newest first). If the library is empty, return
  `None` and let the renderer compose without music.

Both choices are deliberately simple — admin can `reselect-music` /
`regenerate-voiceover` until they're happy.
"""

from __future__ import annotations

import uuid
from typing import List, Optional

from sqlmodel import Session, select

from app.models.music import MusicTrack
from app.models.scenarios import Scenario


def scene_voiceover_texts(scenario: Scenario) -> list[tuple[int, str]]:
    """Per-scene voiceover lines for the scene-aligned TTS path.

    Returns `[(scene_pos, text), …]` for scenes that HAVE narration,
    where `scene_pos` is the 0-based position in idx order (matching
    the render worker's scene ordering). Scenes without narration are
    skipped — no clip is synthesized for them, and the compose voice
    bus simply leaves silence over those scenes.
    """
    if not scenario.scenario_json:
        return []
    scenes = [
        s
        for s in (scenario.scenario_json.get("scenes") or [])
        if isinstance(s, dict) and s.get("idx") is not None
    ]
    scenes.sort(key=lambda s: s.get("idx"))
    out: list[tuple[int, str]] = []
    for pos, scene in enumerate(scenes):
        text = (scene.get("voiceover") or "").strip()
        if text:
            out.append((pos, text.rstrip(".!?") + "."))
    return out


def scene_voiceover_filename(scenario_id: uuid.UUID, version: int, scene_pos: int) -> str:
    return f"voiceover-{scenario_id}-v{version}-scene-{scene_pos:02d}.mp3"


def build_voiceover_script(scenario: Scenario) -> str:
    """Concatenate scene[*].voiceover into a single TTS-ready string.

    Empty/missing scene voiceovers contribute a short pause marker. This is
    intentional — the script reads naturally even when only some scenes
    carry narration, and ElevenLabs respects punctuation for pacing.
    """
    if not scenario.scenario_json:
        return ""
    parts: list[str] = []
    for scene in scenario.scenario_json.get("scenes") or []:
        voiceover = (scene.get("voiceover") or "").strip()
        if voiceover:
            parts.append(voiceover.rstrip(".!?") + ".")
        else:
            # Empty pause — punctuation gap.
            parts.append("...")
    return " ".join(parts).strip()


def select_music_for_scenario(session: Session, scenario: Scenario) -> Optional[MusicTrack]:
    """Pick one music track that fits the scenario.

    Selection priority:
    1. Track whose `mood` array overlaps with `scenario.scenario_json.music.mood`.
    2. Any track in the project's library (newest first).
    3. None when the library is empty.
    """
    desired_moods: list[str] = []
    if scenario.scenario_json:
        music_block = scenario.scenario_json.get("music") or {}
        moods = music_block.get("mood")
        if isinstance(moods, list):
            desired_moods = [str(m).lower() for m in moods if m]
        elif isinstance(moods, str):
            desired_moods = [moods.lower()]

    stmt = (
        select(MusicTrack)
        .where(MusicTrack.project_id == scenario.project_id)
        .order_by(MusicTrack.created_at.desc())
    )
    candidates: List[MusicTrack] = list(session.exec(stmt).all())
    if not candidates:
        return None

    if desired_moods:
        for track in candidates:
            track_moods = [str(m).lower() for m in (track.mood or [])]
            if any(m in track_moods for m in desired_moods):
                return track

    return candidates[0]


def voiceover_filename(scenario_id: uuid.UUID, version: int) -> str:
    return f"scenario-{scenario_id}-voiceover-v{version}.mp3"
