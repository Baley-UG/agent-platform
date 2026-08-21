"""Step-graph authoring — which steps exist per technique."""

from __future__ import annotations

import uuid

from app.models.remake_shots import RemakeShot
from app.services import remake_steps_author as author


class _FakeRemake:
    def __init__(self):
        self.id = uuid.uuid4()


def _shot(idx, technique):
    return RemakeShot(remake_id=uuid.uuid4(), idx=idx, start_sec=0.0, end_sec=2.0, technique=technique)


def test_analysis_chain_shape():
    steps = author.build_analysis_steps(uuid.uuid4())
    kinds = [s.kind for s in steps]
    assert kinds == ["probe", "scene_detect", "frame_extract", "asr", "tag_shots", "author_plan"]
    # All global (no shot).
    assert all(s.shot_id is None for s in steps)
    # frame_extract ∥ asr share seq 2.
    seqs = {s.kind: s.seq for s in steps}
    assert seqs["frame_extract"] == seqs["asr"] == 2
    assert seqs["author_plan"] > seqs["tag_shots"] > seqs["frame_extract"]


def test_copy_is_a_single_cut():
    steps = author._shot_steps(uuid.uuid4(), _shot(0, "copy"))
    assert [s.kind for s in steps] == ["cut"]


def test_erase_is_cut_then_erase_then_normalize():
    steps = author._shot_steps(uuid.uuid4(), _shot(0, "erase"))
    assert [s.kind for s in steps] == ["cut", "erase", "normalize"]
    assert [s.seq for s in steps] == [0, 1, 2]


def test_reframe_parallel_keyframes():
    steps = author._shot_steps(uuid.uuid4(), _shot(0, "reframe"))
    kinds = [s.kind for s in steps]
    assert "keyframe_edit_start" in kinds and "keyframe_edit_end" in kinds
    kf = [s for s in steps if s.kind.startswith("keyframe_edit")]
    assert all(s.seq == 0 for s in kf)  # parallel
    assert next(s.seq for s in steps if s.kind == "i2v") == 1


def test_drop_has_no_steps():
    assert author._shot_steps(uuid.uuid4(), _shot(0, "drop")) == []


def test_render_steps_include_one_global_compose():
    remake = _FakeRemake()
    shots = [_shot(0, "copy"), _shot(1, "erase"), _shot(2, "drop")]
    steps = author.build_render_steps(remake, shots)
    compose = [s for s in steps if s.kind == "compose"]
    assert len(compose) == 1
    assert compose[0].shot_id is None
    # drop contributed no steps.
    assert not any(s.shot_id == shots[2].id for s in steps)


def test_clamp_folds_generative_into_erase():
    for tech in ("restyle", "reframe"):
        shot = _shot(0, tech)
        author.clamp_shot_for_phase1(shot)
        assert shot.technique == "erase"
    keep = _shot(0, "copy")
    author.clamp_shot_for_phase1(keep)
    assert keep.technique == "copy"
