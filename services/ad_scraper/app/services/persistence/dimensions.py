"""Upserts for `ad_dimensions` and `ad_material_dimensions`.

The API sends six parallel facet arrays per material. Their wire key
differs from our storage `kind` in one case (`resourceElement` →
`resource_element`), and their identity field differs in another (`area`
is keyed by `cc`, everything else by `id`) — both handled by the
`_FACETS` table below rather than by branching at each call site.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Tuple

from sqlalchemy import text as sql_text
from sqlmodel import Session

# (payload key, storage kind, identity field)
_FACETS: Tuple[Tuple[str, str, str], ...] = (
    ("media", "media", "id"),
    ("channel", "channel", "id"),
    ("area", "area", "cc"),
    ("format", "format", "id"),
    ("platform", "platform", "id"),
    ("resourceElement", "resource_element", "id"),
)

_UPSERT_DIMENSION = sql_text("""
    INSERT INTO ad_dimensions (kind, code, name, icon, description, parent_code, first_seen_at, last_seen_at)
    VALUES (:kind, :code, :name, :icon, :description, :parent_code, :now, :now)
    ON CONFLICT (kind, code) DO UPDATE SET
        name         = COALESCE(EXCLUDED.name, ad_dimensions.name),
        icon         = COALESCE(EXCLUDED.icon, ad_dimensions.icon),
        description  = COALESCE(EXCLUDED.description, ad_dimensions.description),
        parent_code  = COALESCE(EXCLUDED.parent_code, ad_dimensions.parent_code),
        last_seen_at = EXCLUDED.last_seen_at
    """)

_INSERT_EDGE = sql_text("""
    INSERT INTO ad_material_dimensions (material_id, kind, code)
    VALUES (:material_id, :kind, :code)
    ON CONFLICT DO NOTHING
    """)


def _coerce_code(value: Any) -> str | None:
    """Normalise a facet identity to text.

    `area` arrives as a country code string, the rest as ints. One text
    column holds both; `None` and empty string mean "no identity", which
    we drop rather than store as a phantom row.
    """
    if value is None:
        return None
    code = str(value).strip()
    return code or None


def extract_dimensions(material: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Flatten a material's six facet arrays into one list of dimension dicts.

    Pure — no DB access — so the mapping can be unit-tested against a
    captured payload.
    """
    out: List[Dict[str, Any]] = []
    seen: set[Tuple[str, str]] = set()

    for payload_key, kind, id_field in _FACETS:
        entries = material.get(payload_key)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            code = _coerce_code(entry.get(id_field))
            if code is None:
                continue
            key = (kind, code)
            if key in seen:
                continue
            seen.add(key)
            out.append(
                {
                    "kind": kind,
                    "code": code,
                    "name": entry.get("name"),
                    "icon": entry.get("icon"),
                    "description": entry.get("description"),
                    "parent_code": _coerce_code(entry.get("parentId")),
                }
            )
    return out


def upsert_dimensions(session: Session, material_id: str, dimensions: Iterable[Dict[str, Any]]) -> int:
    """Upsert facet rows and link them to the material. Returns edge count.

    Dimension rows are written before the edges because the edge table has
    a composite FK onto `(kind, code)`.
    """
    now = datetime.now(timezone.utc)
    count = 0
    for dim in dimensions:
        session.execute(
            _UPSERT_DIMENSION,
            {
                "kind": dim["kind"],
                "code": dim["code"],
                "name": dim.get("name"),
                "icon": dim.get("icon"),
                "description": dim.get("description"),
                "parent_code": dim.get("parent_code"),
                "now": now,
            },
        )
        session.execute(
            _INSERT_EDGE,
            {"material_id": material_id, "kind": dim["kind"], "code": dim["code"]},
        )
        count += 1
    return count
