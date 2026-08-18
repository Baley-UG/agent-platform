"""Read endpoints for advertised entities (apps, brands, sites, dramas)."""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from sqlmodel import Session

from app.api.v1.deps import get_read_session, require_api_key
from app.services import queries

router = APIRouter(dependencies=[Depends(require_api_key)])


@router.get("")
def list_advertisers(
    kind: Optional[str] = Query(default=None, description="App | AppBrand | Website | Playlet | Novel"),
    search: Optional[str] = Query(default=None, description="Substring match on name or alias."),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_read_session),
) -> List[Dict[str, Any]]:
    """List advertisers with a creative count, busiest first."""
    return queries.list_advertisers(session, kind=kind, search=search, limit=limit, offset=offset)


@router.get("/{advertiser_id}/materials")
def list_advertiser_materials(
    advertiser_id: str,
    sort: str = Query(default=queries.DEFAULT_SORT),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_read_session),
) -> List[Dict[str, Any]]:
    """Every creative this advertiser is running."""
    return queries.search_materials(
        session,
        advertiser_id=advertiser_id,
        sort=sort if sort in queries.SORT_OPTIONS else queries.DEFAULT_SORT,
        limit=limit,
        offset=offset,
    )
