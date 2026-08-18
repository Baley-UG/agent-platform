"""Read endpoint for facet values — feeds an admin panel's filter dropdowns.

Facet values are discovered from ingested data rather than seeded: the
platform's media/channel/area/format/platform/resourceElement vocabularies
are not published anywhere we can read, so what we know is what we have
seen. Usage counts come along so a panel can hide facets with no data.
"""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlmodel import Session

from app.api.v1.deps import get_read_session, require_api_key
from app.models.dimension import DIMENSION_KINDS
from app.services import queries

router = APIRouter(dependencies=[Depends(require_api_key)])


@router.get("")
def list_dimensions(
    kind: Optional[str] = Query(default=None, description=f"One of: {', '.join(DIMENSION_KINDS)}"),
    limit: int = Query(default=500, ge=1, le=5000),
    session: Session = Depends(get_read_session),
) -> List[Dict[str, Any]]:
    """List known facet values with usage counts."""
    if kind and kind not in DIMENSION_KINDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"kind must be one of {list(DIMENSION_KINDS)}",
        )
    return queries.list_dimensions(session, kind=kind, limit=limit)
