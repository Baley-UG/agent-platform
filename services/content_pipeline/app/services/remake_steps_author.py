"""Authoring of `remake_steps` rows.

Pure(ish) functions that decide WHICH steps exist for a remake at each
phase boundary. Kept separate from the reconciler so the step graph is
unit-testable without Redis or a running worker.

Two phases:
  - `build_analysis_steps` — enqueued at remake creation.
  - `build_render_steps`   — authored when the operator approves the plan
                             (Gate 1). Reads each shot's `technique`.

`seq` orders steps WITHIN a scope (a scope = one shot, or the global
shot_id-NULL scope). The reconciler enqueues a step only once every
lower-seq step in the same scope has succeeded/skipped. `compose` has an
extra cross-scope dependency (all shots ready) handled in the reconciler.
"""

from __future__ import annotations

import uuid
from typing import List

from app.models.remake_shots import RemakeShot
from app.models.remake_steps import RemakeStep

# Phase-1 techniques that need no AI generation step — the cut clip is
# the shot output directly (copy) or after one erase pass (erase).
_PHASE1_TECHNIQUES = {"copy", "erase", "drop"}


def build_analysis_steps(remake_id: uuid.UUID) -> List[RemakeStep]:
    """The global analysis chain, authored at creation.

    probe → scene_detect → (frame_extract ∥ asr) → tag_shots → author_plan

    scene_detect writes the `remake_shots` rows (boundaries known there);
    frame_extract + tag_shots then fill per-shot frames/tags; author_plan
    assigns techniques and writes `remakes.plan_json`.
    """
    def step(kind: str, seq: int, max_attempts: int = 2) -> RemakeStep:
        return RemakeStep(remake_id=remake_id, shot_id=None, kind=kind, seq=seq, max_attempts=max_attempts)

    return [
        step("probe", 0),
        step("scene_detect", 1),
        step("frame_extract", 2),
        step("asr", 2, max_attempts=1),  # ASR is best-effort; a failure shouldn't block the plan
        step("tag_shots", 3),
        step("author_plan", 4),
    ]


def _shot_steps(remake_id: uuid.UUID, shot: RemakeShot) -> List[RemakeStep]:
    """Steps for one shot, per its technique.

    seq numbering is per-shot (each shot is its own scope), so parallel
    keyframe edits share seq=0 and run concurrently.
    """
    def step(kind: str, seq: int) -> RemakeStep:
        return RemakeStep(remake_id=remake_id, shot_id=shot.id, kind=kind, seq=seq)

    t = shot.technique
    if t == "drop":
        return []
    if t == "copy":
        # One pass produces the normalized clip directly.
        return [step("cut", 0)]
    if t == "erase":
        return [step("cut", 0), step("erase", 1), step("normalize", 2)]
    if t == "restyle":
        return [step("cut", 0), step("restyle", 1), step("normalize", 2)]
    if t == "reframe":
        # Two keyframe edits run in parallel (seq 0), then i2v, then normalize.
        return [
            step("keyframe_edit_start", 0),
            step("keyframe_edit_end", 0),
            step("i2v", 1),
            step("normalize", 2),
        ]
    return [step("cut", 0)]  # unknown technique degrades to a verbatim cut


def build_render_steps(
    remake, shots: List[RemakeShot], *, phase1_only: bool = True
) -> List[RemakeStep]:
    """All render steps for an approved plan: per-shot steps + the global
    compose (and optional tts/upscale).

    `phase1_only` clamps `restyle`/`reframe` down to `erase` — the
    generative techniques ship in Phase 2. The clamp happens on the SHOT
    (so the UI reflects it too); callers pass the already-clamped shots.
    """
    steps: List[RemakeStep] = []
    for shot in shots:
        steps.extend(_shot_steps(remake.id, shot))

    # Global compose. Its cross-scope dependency (all non-dropped shots
    # ready) is enforced in the reconciler, not by seq. A high seq keeps
    # it after any global audio step that might be added in Phase 3.
    steps.append(RemakeStep(remake_id=remake.id, shot_id=None, kind="compose", seq=10))
    return steps


def clamp_shot_for_phase1(shot: RemakeShot) -> None:
    """In Phase 1 the generative techniques aren't wired yet; fold them
    into `erase` (which still removes branding, just without a reshoot).
    Mutates the shot in place."""
    if shot.technique in ("restyle", "reframe"):
        shot.technique = "erase"
