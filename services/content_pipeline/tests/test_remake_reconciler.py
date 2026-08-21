"""Reconciler decision logic — the heterogeneous-state matrices that
killed v1.

v1 advanced a scenario by SET-EQUALITY over child statuses, so any mix
like {ready, pending} or {video_ready, failed} matched no branch and
hung forever. These tests pin the replacement: `_derive_shot_status` and
`_deps_met` are total (every mix maps to a defined outcome) and a failed
step never blocks a sibling.

The full DB round-trip of `advance()` (with_for_update + JSONB) is
covered by the live-stack e2e; here we test the pure decision core with
in-memory model instances.
"""

from __future__ import annotations

import uuid

from app.models.remake_shots import RemakeShot
from app.models.remake_steps import RemakeStep
from app.services import remake_reconciler as rc


def _shot(technique="copy"):
    return RemakeShot(remake_id=uuid.uuid4(), idx=0, start_sec=0.0, end_sec=2.0, technique=technique)


def _step(kind="cut", seq=0, status="pending", shot_id=None, max_attempts=2, attempts=0):
    return RemakeStep(
        remake_id=uuid.uuid4(), shot_id=shot_id, kind=kind, seq=seq,
        status=status, max_attempts=max_attempts, attempts=attempts,
    )


# ---------------------------------------------------------------------------
# _derive_shot_status — total over every status mix
# ---------------------------------------------------------------------------


def test_drop_shot_is_dropped_regardless_of_steps():
    assert rc._derive_shot_status(_shot("drop"), []) == "dropped"


def test_no_steps_is_planned():
    assert rc._derive_shot_status(_shot(), []) == "planned"


def test_all_succeeded_is_ready():
    steps = [_step(status="succeeded"), _step(kind="normalize", seq=2, status="succeeded")]
    assert rc._derive_shot_status(_shot("erase"), steps) == "ready"


def test_succeeded_plus_skipped_is_ready():
    steps = [_step(status="succeeded"), _step(kind="normalize", seq=2, status="skipped")]
    assert rc._derive_shot_status(_shot("erase"), steps) == "ready"


def test_any_failed_is_needs_attention():
    # THE v1 killer: {succeeded, failed} used to match no branch → hang.
    steps = [_step(status="succeeded"), _step(kind="erase", seq=1, status="failed")]
    assert rc._derive_shot_status(_shot("erase"), steps) == "needs_attention"


def test_failed_plus_pending_is_needs_attention():
    steps = [_step(status="failed"), _step(kind="normalize", seq=2, status="pending")]
    assert rc._derive_shot_status(_shot("erase"), steps) == "needs_attention"


def test_running_mix_is_rendering():
    # {succeeded, queued} — another mix v1 couldn't represent.
    steps = [_step(status="succeeded"), _step(kind="erase", seq=1, status="queued")]
    assert rc._derive_shot_status(_shot("erase"), steps) == "rendering"


def test_pending_only_is_planned():
    steps = [_step(status="pending"), _step(kind="normalize", seq=2, status="pending")]
    assert rc._derive_shot_status(_shot("erase"), steps) == "planned"


def test_every_status_mix_maps_to_a_defined_outcome():
    """Exhaustive: no combination of step statuses is unreachable."""
    from itertools import combinations_with_replacement

    universe = ["pending", "queued", "running", "succeeded", "failed", "skipped"]
    valid = {"planned", "rendering", "ready", "needs_attention", "dropped"}
    for n in (1, 2, 3):
        for combo in combinations_with_replacement(universe, n):
            steps = [_step(status=st, seq=i) for i, st in enumerate(combo)]
            out = rc._derive_shot_status(_shot(), steps)
            assert out in valid, f"{combo} → {out}"


# ---------------------------------------------------------------------------
# _deps_met — seq ordering within a scope
# ---------------------------------------------------------------------------


def _by_scope(steps):
    d: dict = {}
    for s in steps:
        d.setdefault(s.shot_id, []).append(s)
    return d


def test_first_step_has_no_deps():
    sid = uuid.uuid4()
    s0 = _step(kind="cut", seq=0, shot_id=sid)
    assert rc._deps_met(s0, _by_scope([s0]), {}) is True


def test_later_step_waits_for_earlier():
    sid = uuid.uuid4()
    s0 = _step(kind="cut", seq=0, status="running", shot_id=sid)
    s1 = _step(kind="erase", seq=1, status="pending", shot_id=sid)
    scope = _by_scope([s0, s1])
    assert rc._deps_met(s1, scope, {}) is False
    s0.status = "succeeded"
    assert rc._deps_met(s1, scope, {}) is True


def test_skipped_predecessor_unblocks():
    sid = uuid.uuid4()
    s0 = _step(kind="cut", seq=0, status="skipped", shot_id=sid)
    s1 = _step(kind="normalize", seq=2, status="pending", shot_id=sid)
    assert rc._deps_met(s1, _by_scope([s0, s1]), {}) is True


def test_parallel_same_seq_do_not_block_each_other():
    sid = uuid.uuid4()
    a = _step(kind="keyframe_edit_start", seq=0, status="pending", shot_id=sid)
    b = _step(kind="keyframe_edit_end", seq=0, status="pending", shot_id=sid)
    scope = _by_scope([a, b])
    assert rc._deps_met(a, scope, {}) is True
    assert rc._deps_met(b, scope, {}) is True


def test_scopes_are_independent():
    """A step in shot A is not blocked by an unfinished step in shot B."""
    a, b = uuid.uuid4(), uuid.uuid4()
    a0 = _step(kind="cut", seq=0, status="pending", shot_id=a)
    b0 = _step(kind="cut", seq=0, status="running", shot_id=b)
    assert rc._deps_met(a0, _by_scope([a0, b0]), {}) is True


# ---------------------------------------------------------------------------
# compose — the one cross-scope dependency
# ---------------------------------------------------------------------------


def test_compose_waits_for_all_shots_ready():
    a, b = uuid.uuid4(), uuid.uuid4()
    compose = _step(kind="compose", seq=10, shot_id=None)
    scope = _by_scope([compose])
    assert rc._deps_met(compose, scope, {a: True, b: False}) is False
    assert rc._deps_met(compose, scope, {a: True, b: True}) is True


def test_compose_with_no_shots_is_not_ready():
    compose = _step(kind="compose", seq=10, shot_id=None)
    assert rc._deps_met(compose, _by_scope([compose]), {}) is False


def test_compose_ignores_dropped_shots():
    # dropped shots never enter `shots_ready`, so a deck of one kept +
    # one dropped is composable once the kept one is ready.
    kept = uuid.uuid4()
    compose = _step(kind="compose", seq=10, shot_id=None)
    assert rc._deps_met(compose, _by_scope([compose]), {kept: True}) is True
