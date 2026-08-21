"""Up-front cost estimation for a remake.

Computed in code (never guessed by the LLM) so the operator sees an
honest $ figure at Gate 1. Prices are per-technique defaults from the
fal.ai catalogue (Aug 2026); they intentionally live here rather than in
`model_routes` so an estimate never requires a DB round-trip per shot.
The ledger (`generation_calls`) records the ACTUAL cost as steps run.
"""

from __future__ import annotations

from typing import List

from sqlmodel import Session

from app.models.remake_shots import RemakeShot
from app.models.remakes import Remake

# Rough per-technique prices. Video-second rates × the shot duration;
# flat rates as-is. Kept conservative (round up) so estimates don't
# undershoot.
_ERASE_FLAT_USD = 0.05          # VOID inpaint, flat per clip
_RESTYLE_PER_SEC_USD = 0.168    # Kling O1 v2v
_REFRAME_KEYFRAME_USD = 0.15    # nano-banana edit, ×2 (start+end)
_REFRAME_I2V_PER_SEC_USD = 0.084  # Kling O3 i2v
# Analysis: whisper is a rounding error; tag+plan are a couple cents.
_ANALYSIS_FLAT_USD = 0.05


def estimate_shot(shot: RemakeShot) -> float:
    dur = float(shot.end_sec) - float(shot.start_sec)
    dur = max(dur, 0.0)
    t = shot.technique
    if t in ("copy", "drop"):
        return 0.0
    if t == "erase":
        return _ERASE_FLAT_USD
    if t == "restyle":
        return round(_RESTYLE_PER_SEC_USD * dur, 4)
    if t == "reframe":
        return round(2 * _REFRAME_KEYFRAME_USD + _REFRAME_I2V_PER_SEC_USD * dur, 4)
    return 0.0


def estimate_remake(session: Session, remake: Remake) -> float:
    """Stamp `est_cost_usd` on every shot + the remake. Returns the total."""
    from app.services.remakes import shots_for  # local import to avoid a cycle

    shots: List[RemakeShot] = shots_for(session, remake.id)
    total = _ANALYSIS_FLAT_USD
    for shot in shots:
        c = estimate_shot(shot)
        shot.est_cost_usd = c
        session.add(shot)
        total += c
    remake.est_cost_usd = round(total, 4)
    session.add(remake)
    session.flush()
    return remake.est_cost_usd
