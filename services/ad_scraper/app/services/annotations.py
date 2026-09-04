"""AI annotations on ad creatives — script (scenario) + summary.

Filled by an agent that watched the creative (via the MCP `annotate_ad`
tool or REST), never by the upstream ingest. Kept separate from
`persistence/materials.py` because the upsert there deliberately owns
only upstream-sourced columns — an ingest re-run must never clobber an
annotation.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlmodel import Session

from app.models.material import Material


def annotate(
    session: Session,
    material_id: str,
    *,
    script: Optional[str] = None,
    summary: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Set/replace the script and/or summary of one material.

    Only the fields passed non-None change; passing an empty string
    clears a field. Returns a compact dict, or None when the material
    doesn't exist.
    """
    row = session.get(Material, material_id)
    if row is None:
        return None
    if script is not None:
        row.script = script.strip() or None
    if summary is not None:
        row.summary = summary.strip() or None
    row.annotated_at = datetime.now(timezone.utc)
    session.add(row)
    session.flush()
    return {
        "material_id": row.id,
        "has_script": bool(row.script),
        "has_summary": bool(row.summary),
        "annotated_at": str(row.annotated_at),
    }
