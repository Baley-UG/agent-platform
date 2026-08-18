"""ad_advertisers, ad_material_advertisers — who is running the creative.

The API's `campaign[]` array holds every entity advertising with a given
creative. It is a union type — `App`, `AppBrand`, `Website`, `Playlet`,
`Novel` — and it fans out hard: one creative in the sample carried 66
entries, because ad networks resell the same drama trailer to dozens of
apps and landing domains.

`kind` comes from GraphQL `__typename`, which our query requests
explicitly (see `app/services/youcloud/queries.py`). Do NOT go back to
inferring it from the payload shape: the observed heuristic
(`types`+`developer` → AppBrand, `type=401` → Website, `type=400` →
Playlet) is undocumented and silently wrong the moment the platform adds
a variant.
"""

from datetime import datetime
from typing import List, Optional

import sqlalchemy as sa
from sqlalchemy import Column
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlmodel import Field, SQLModel

from app.models.base import utcnow


class Advertiser(SQLModel, table=True):
    """One advertised entity: an app, a brand, a landing site, a drama."""

    __tablename__ = "ad_advertisers"

    # Opaque base64-ish token, e.g. `yscR4ZQ5GNMdwbnrFs5Now==`. Not a
    # number, not a uuid — text is the only honest column type.
    id: str = Field(primary_key=True, max_length=128)

    kind: Optional[str] = Field(
        default=None,
        max_length=32,
        index=True,
        description="GraphQL __typename: App | AppBrand | Website | Playlet | Novel.",
    )
    type: Optional[int] = Field(default=None, description="Raw API `type` code (400, 401, ...).")
    name: Optional[str] = Field(default=None, max_length=512, index=True)
    icon: Optional[str] = Field(default=None)

    # AppBrand-only fields; NULL for websites and playlets.
    types: Optional[List[int]] = Field(default=None, sa_column=Column("types", ARRAY(sa.Integer), nullable=True))
    # `alias` is an ARRAY, not a string: the API returns every localised
    # store name for the app ("All Video Downloader App", "المسلسلات
    # القصيرة DramaBox", ...) — up to ten of them. Typed as a scalar it
    # silently overflowed a varchar(512).
    alias: Optional[List[str]] = Field(default=None, sa_column=Column("alias", ARRAY(sa.Text), nullable=True))
    gp_app_url: Optional[str] = Field(default=None)
    ios_app_url: Optional[str] = Field(default=None)
    minis_type: Optional[int] = Field(default=None)

    developer_id: Optional[str] = Field(default=None, max_length=128, index=True)
    developer_name: Optional[str] = Field(default=None, max_length=512)
    developer_area_cc: Optional[str] = Field(default=None, max_length=8)

    raw: Optional[dict] = Field(default=None, sa_column=Column("raw", JSONB, nullable=True))
    first_seen_at: datetime = Field(default_factory=utcnow)
    last_seen_at: datetime = Field(default_factory=utcnow)


class MaterialAdvertiser(SQLModel, table=True):
    """Edge between a material and one advertised entity."""

    __tablename__ = "ad_material_advertisers"
    __table_args__ = (
        # "which creatives is this advertiser running" — the reverse of the PK.
        sa.Index("ix_ad_material_advertisers_advertiser", "advertiser_id", "material_id"),
    )

    material_id: str = Field(foreign_key="ad_materials.id", primary_key=True, max_length=64)
    advertiser_id: str = Field(foreign_key="ad_advertisers.id", primary_key=True, max_length=128)
