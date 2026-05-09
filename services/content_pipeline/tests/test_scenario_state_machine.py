"""Scenario state machine + aspect group derivation."""

import pytest

from app.models.scenarios import Scenario
from app.services.scenarios import (
    InvalidStateTransition,
    _derive_aspect_groups,
    transition,
)


def test_aspect_groups_dedup_9_16_family():
    groups = _derive_aspect_groups(["ig_reels", "tiktok", "ig_story"])
    assert groups == ["9:16"]


def test_aspect_groups_mix():
    groups = _derive_aspect_groups(["ig_reels", "ig_feed_45", "tiktok", "ig_feed_11"])
    assert set(groups) == {"9:16", "4:5", "1:1"}


def test_aspect_groups_unknown_variant_skipped():
    assert _derive_aspect_groups(["unknown_platform"]) == []


def test_transition_draft_to_analyzing():
    s = Scenario(project_id="00000000-0000-0000-0000-000000000000", status="draft")
    transition(s, "analyzing")
    assert s.status == "analyzing"


def test_transition_disallows_skipping_states():
    s = Scenario(project_id="00000000-0000-0000-0000-000000000000", status="draft")
    with pytest.raises(InvalidStateTransition):
        transition(s, "approved")


def test_transition_unknown_status_rejected():
    s = Scenario(project_id="00000000-0000-0000-0000-000000000000", status="draft")
    with pytest.raises(InvalidStateTransition, match="unknown status"):
        transition(s, "totally_made_up")


def test_transition_idempotent_self_loop():
    s = Scenario(project_id="00000000-0000-0000-0000-000000000000", status="approved")
    transition(s, "approved")  # no-op, no exception
    assert s.status == "approved"


def test_failed_can_restart_via_analyzing_or_draft():
    s = Scenario(project_id="00000000-0000-0000-0000-000000000000", status="failed")
    transition(s, "analyzing")
    assert s.status == "analyzing"


def test_approved_can_start_render_pipeline():
    s = Scenario(project_id="00000000-0000-0000-0000-000000000000", status="approved")
    transition(s, "generating_images")
    assert s.status == "generating_images"
