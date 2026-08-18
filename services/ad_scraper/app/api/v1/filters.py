"""The filter schema endpoint — one source of truth for the panel's form.

Split from `dimensions.py` on purpose. `/dimensions` answers "what facet
values have we ingested"; this answers "what can a job ask for, and what are
the legal values" — which includes constraints and traps that are not facet
values at all (the page ceiling, the area pattern, the keys the worker owns).
"""

from typing import Any, Dict

from fastapi import APIRouter, Depends
from sqlmodel import Session

from app.api.v1.deps import get_read_session, require_api_key
from app.services import filter_schema

router = APIRouter(dependencies=[Depends(require_api_key)])


@router.get("")
def get_filter_schema(session: Session = Depends(get_read_session)) -> Dict[str, Any]:
    """Every filter a job can carry, with its options and constraints.

    Replaces the hand-maintained option list a panel would otherwise keep.
    Facet values merge what we have actually ingested (with usage counts)
    over a small measured seed, so a fresh database still yields a usable
    form on day one.
    """
    return filter_schema.build(session)
