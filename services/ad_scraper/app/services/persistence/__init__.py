"""Persistence helpers — one module per table family.

Every write is a single `INSERT ... ON CONFLICT DO UPDATE` statement, and
the payload→column mapping is a pure function next to it so it can be
tested against a captured API response without a database.
"""

from app.services.persistence.advertisers import extract_advertisers, upsert_advertisers
from app.services.persistence.dimensions import extract_dimensions, upsert_dimensions
from app.services.persistence.materials import (
    UpsertResult,
    build_material_params,
    extract_resources,
    upsert_material,
)

__all__ = [
    "extract_advertisers",
    "upsert_advertisers",
    "extract_dimensions",
    "upsert_dimensions",
    "extract_resources",
    "build_material_params",
    "upsert_material",
    "UpsertResult",
]
