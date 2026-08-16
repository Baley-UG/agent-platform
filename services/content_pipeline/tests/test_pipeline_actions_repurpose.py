"""The action matrix the admin panel's stepper reads.

Two things are pinned here: repurpose gets its own leg (no image/video
synthesis), and the recreate/brand_build matrices are byte-identical to
what they were before repurpose existed.
"""

from __future__ import annotations

import pytest

from app.services import scenarios as svc


class FakeScenario:
    def __init__(self, status: str, production_mode: str = "recreate"):
        self.status = status
        self.production_mode = production_mode
        self.reference_id = None
        self.segment_plan = None


@pytest.fixture(autouse=True)
def stub_source_kind(monkeypatch):
    """`scenario_source_kind` hits the DB; the matrix under test only
    needs the label."""
    monkeypatch.setattr(svc, "scenario_source_kind", lambda session, scenario: "reel")


def actions(status: str, mode: str = "recreate") -> dict:
    return svc.pipeline_actions(None, FakeScenario(status, mode))


# ---------------------------------------------------------------------------
# repurpose leg
# ---------------------------------------------------------------------------


def test_repurpose_never_offers_image_or_video_synthesis():
    for status in ("approved", "segments_ready", "audio_ready", "composing"):
        a = actions(status, "repurpose")
        assert a["can_start_images"] is False
        assert a["can_start_videos"] is False
        assert a["needs_video_generation"] is False


def test_repurpose_cuts_from_approved():
    a = actions("approved", "repurpose")
    assert a["can_start_segments"] is True
    assert a["needs_segment_cut"] is True


def test_repurpose_cannot_cut_before_approval():
    assert actions("pending_review", "repurpose")["can_start_segments"] is False


def test_repurpose_composes_straight_from_segments_ready():
    """The source audio ships as-is, so no TTS pass is required."""
    a = actions("segments_ready", "repurpose")
    assert a["can_start_compose"] is True
    assert a["needs_audio_generation"] is False


def test_repurpose_voiceover_is_available_but_optional():
    assert actions("segments_ready", "repurpose")["can_start_audio"] is True


def test_repurpose_composes_after_an_optional_voiceover_pass():
    assert actions("audio_ready", "repurpose")["can_start_compose"] is True


def test_repurpose_cannot_compose_mid_cut():
    assert actions("cutting_segments", "repurpose")["can_start_compose"] is False


def test_repurpose_final_approval_gate_matches_the_others():
    assert actions("final_pending_review", "repurpose")["can_approve_final"] is True


# ---------------------------------------------------------------------------
# regression — legacy modes unchanged
# ---------------------------------------------------------------------------


_LEGACY_KEYS = (
    "can_start_images",
    "can_start_videos",
    "can_start_audio",
    "can_start_compose",
    "can_approve_final",
    "needs_video_generation",
    "needs_audio_generation",
)

_EXPECTED_REEL = {
    "approved": {"can_start_images": True},
    "images_ready": {"can_start_videos": True, "can_start_compose": False},
    "videos_ready": {"can_start_audio": True, "can_start_compose": False},
    "audio_ready": {"can_start_compose": True},
    "final_pending_review": {"can_approve_final": True},
}


@pytest.mark.parametrize("status,expected", _EXPECTED_REEL.items())
def test_recreate_matrix_unchanged(status, expected):
    a = actions(status, "recreate")
    for key, value in expected.items():
        assert a[key] is value, f"{status}.{key}"


@pytest.mark.parametrize("status", list(_EXPECTED_REEL))
def test_brand_build_matches_recreate(status):
    recreate = actions(status, "recreate")
    brand = actions(status, "brand_build")
    assert {k: recreate[k] for k in _LEGACY_KEYS} == {k: brand[k] for k in _LEGACY_KEYS}


@pytest.mark.parametrize("mode", ["recreate", "brand_build", "inspire"])
def test_legacy_modes_never_offer_segment_cutting(mode):
    for status in ("approved", "images_ready", "videos_ready"):
        a = actions(status, mode)
        assert a["can_start_segments"] is False
        assert a["needs_segment_cut"] is False


def test_every_mode_returns_the_same_key_set():
    """The panel destructures this dict; a missing key would render as
    `undefined` and silently hide a button."""
    keys = {mode: set(actions("approved", mode)) for mode in ("recreate", "repurpose", "inspire")}
    assert keys["recreate"] == keys["repurpose"] == keys["inspire"]


def test_production_mode_is_echoed_back():
    assert actions("approved", "repurpose")["production_mode"] == "repurpose"
    assert actions("approved", "recreate")["production_mode"] == "recreate"


def test_missing_production_mode_defaults_to_recreate():
    scenario = FakeScenario("approved")
    scenario.production_mode = None
    assert svc.pipeline_actions(None, scenario)["production_mode"] == "recreate"


# ---------------------------------------------------------------------------
# state machine
# ---------------------------------------------------------------------------


def test_approved_can_enter_either_leg():
    assert "generating_images" in svc._ALLOWED_NEXT["approved"]
    assert "cutting_segments" in svc._ALLOWED_NEXT["approved"]


def test_cut_leg_reaches_compose():
    assert "segments_ready" in svc._ALLOWED_NEXT["cutting_segments"]
    assert "composing" in svc._ALLOWED_NEXT["segments_ready"]


def test_segments_ready_can_re_cut_after_a_plan_edit():
    assert "cutting_segments" in svc._ALLOWED_NEXT["segments_ready"]


def test_new_statuses_are_registered():
    from app.models.scenarios import SCENARIO_STATUSES

    assert "cutting_segments" in SCENARIO_STATUSES
    assert "segments_ready" in SCENARIO_STATUSES


def test_transition_into_cut_leg_is_accepted():
    scenario = FakeScenario("approved", "repurpose")
    svc.transition(scenario, "cutting_segments")
    assert scenario.status == "cutting_segments"


def test_cut_leg_cannot_skip_to_compose():
    scenario = FakeScenario("cutting_segments", "repurpose")
    with pytest.raises(svc.InvalidStateTransition):
        svc.transition(scenario, "composing")
