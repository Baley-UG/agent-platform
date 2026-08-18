"""ad_dimensions, ad_material_dimensions — the generic facet pair.

The API attaches six parallel facet arrays to every material: `media`,
`channel`, `area`, `format`, `platform` and `resourceElement`. They all
share the same wire shape — `{id, name, icon}` plus an occasional
`description` (media) or `parentId` (resourceElement).

Six dedicated lookup tables plus six join tables would be twelve tables
carrying one shape. We collapse them into one `(kind, code)` keyed lookup
and one edge table instead.

Trade-off, stated plainly: we give up per-facet foreign keys and typed
columns. We get back a schema that absorbs a seventh facet without a
migration, and a single index that serves every facet filter. Queries
join `ad_material_dimensions` once per facet being filtered:

    SELECT m.* FROM ad_materials m
    JOIN ad_material_dimensions d1
      ON d1.material_id = m.id AND d1.kind = 'media' AND d1.code = '2'
    JOIN ad_material_dimensions d2
      ON d2.material_id = m.id AND d2.kind = 'area'  AND d2.code = 'TR'
    WHERE m.type = 202;

`code` is text for every kind because `area` is keyed by country code
("TR") while the rest are keyed by integer id ("2"). One column type for
all of them beats a polymorphic pair.
"""

from datetime import datetime
from typing import Optional

import sqlalchemy as sa
from sqlmodel import Field, SQLModel

from app.models.base import utcnow

# The facet kinds the API sends today. Persistence accepts anything —
# this tuple exists so the API layer can validate a `?kind=` query param
# and so a reader knows what to expect.
DIMENSION_KINDS: tuple[str, ...] = (
    "media",
    "channel",
    "area",
    "format",
    "platform",
    "resource_element",
)


class Dimension(SQLModel, table=True):
    """A single facet value — one row per (kind, code)."""

    __tablename__ = "ad_dimensions"

    kind: str = Field(primary_key=True, max_length=32, description="One of DIMENSION_KINDS.")
    code: str = Field(primary_key=True, max_length=64, description="Facet id as text ('2', 'TR', ...).")

    name: Optional[str] = Field(default=None, max_length=255)
    icon: Optional[str] = Field(default=None)
    description: Optional[str] = Field(default=None)
    parent_code: Optional[str] = Field(default=None, max_length=64, description="resourceElement `parentId`.")

    first_seen_at: datetime = Field(default_factory=utcnow)
    last_seen_at: datetime = Field(default_factory=utcnow)


class MaterialDimension(SQLModel, table=True):
    """Edge between a material and one facet value."""

    __tablename__ = "ad_material_dimensions"
    __table_args__ = (
        # Serves the "find materials by facet" direction. The PK already
        # serves the "list a material's facets" direction.
        sa.Index("ix_ad_material_dimensions_kind_code", "kind", "code", "material_id"),
        sa.ForeignKeyConstraint(
            ["kind", "code"],
            ["ad_dimensions.kind", "ad_dimensions.code"],
            name="fk_ad_material_dimensions_dimension",
        ),
    )

    material_id: str = Field(foreign_key="ad_materials.id", primary_key=True, max_length=64)
    kind: str = Field(primary_key=True, max_length=32)
    code: str = Field(primary_key=True, max_length=64)
