"""Upserts for `ad_advertisers` and `ad_material_advertisers`.

`campaign[]` is a GraphQL union and fans out hard — one creative in the
sample carried 66 entries, because ad networks resell the same trailer to
dozens of apps and landing domains. `kind` comes from `__typename`, which
our query requests explicitly; see `queries.py` for why we don't infer it.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List

from sqlalchemy import text as sql_text
from sqlmodel import Session

from app.services.parsing import parse_int

_UPSERT_ADVERTISER = sql_text("""
    INSERT INTO ad_advertisers (
        id, kind, type, name, icon, types, alias, gp_app_url, ios_app_url, minis_type,
        developer_id, developer_name, developer_area_cc, raw, first_seen_at, last_seen_at
    )
    VALUES (
        :id, :kind, :type, :name, :icon, CAST(:types AS integer[]),
        CAST(:alias AS text[]), :gp_app_url, :ios_app_url, :minis_type,
        :developer_id, :developer_name, :developer_area_cc, CAST(:raw AS jsonb), :now, :now
    )
    ON CONFLICT (id) DO UPDATE SET
        kind              = COALESCE(EXCLUDED.kind, ad_advertisers.kind),
        type              = COALESCE(EXCLUDED.type, ad_advertisers.type),
        name              = COALESCE(EXCLUDED.name, ad_advertisers.name),
        icon              = COALESCE(EXCLUDED.icon, ad_advertisers.icon),
        types             = COALESCE(EXCLUDED.types, ad_advertisers.types),
        alias             = COALESCE(EXCLUDED.alias, ad_advertisers.alias),
        gp_app_url        = COALESCE(EXCLUDED.gp_app_url, ad_advertisers.gp_app_url),
        ios_app_url       = COALESCE(EXCLUDED.ios_app_url, ad_advertisers.ios_app_url),
        minis_type        = COALESCE(EXCLUDED.minis_type, ad_advertisers.minis_type),
        developer_id      = COALESCE(EXCLUDED.developer_id, ad_advertisers.developer_id),
        developer_name    = COALESCE(EXCLUDED.developer_name, ad_advertisers.developer_name),
        developer_area_cc = COALESCE(EXCLUDED.developer_area_cc, ad_advertisers.developer_area_cc),
        raw               = COALESCE(EXCLUDED.raw, ad_advertisers.raw),
        last_seen_at      = EXCLUDED.last_seen_at
    """)

_INSERT_EDGE = sql_text("""
    INSERT INTO ad_material_advertisers (material_id, advertiser_id)
    VALUES (:material_id, :advertiser_id)
    ON CONFLICT DO NOTHING
    """)


def _int_list(value: Any) -> List[int] | None:
    """Coerce the AppBrand `types` array; None when there's nothing usable."""
    if not isinstance(value, list):
        return None
    out = [parse_int(v) for v in value]
    out = [v for v in out if v is not None]
    return out or None


def _text_list(value: Any) -> List[str] | None:
    """Coerce the AppBrand `alias` array.

    `alias` is a LIST of localised store names, not a string — an app can
    carry ten of them across locales. A scalar column overflows on the
    first real AppBrand row, so the type here is load-bearing. Tolerates a
    bare string in case the platform ever collapses the field.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return [value] if value.strip() else None
    if not isinstance(value, list):
        return None
    out = [str(v).strip() for v in value if v is not None and str(v).strip()]
    return out or None


def extract_advertisers(material: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Flatten `campaign[]` into advertiser dicts. Pure, unit-testable.

    Entries without an `id` are dropped: without identity we cannot dedupe
    them, and storing them would grow a pile of unjoinable rows.
    """
    entries = material.get("campaign")
    if not isinstance(entries, list):
        return []

    out: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        advertiser_id = entry.get("id")
        if not advertiser_id:
            continue
        advertiser_id = str(advertiser_id)
        if advertiser_id in seen:
            continue
        seen.add(advertiser_id)

        developer = entry.get("developer") if isinstance(entry.get("developer"), dict) else {}
        dev_area = developer.get("area") if isinstance(developer.get("area"), dict) else {}

        out.append(
            {
                "id": advertiser_id,
                # `__typename` is authoritative. When absent (an older
                # cached query, a schema change) we store NULL rather than
                # guessing — a NULL is visibly missing, a wrong guess isn't.
                "kind": entry.get("__typename"),
                "type": parse_int(entry.get("type")),
                "name": entry.get("name"),
                "icon": entry.get("icon"),
                "types": _int_list(entry.get("types")),
                "alias": _text_list(entry.get("alias")),
                "gp_app_url": entry.get("gp_app_url"),
                "ios_app_url": entry.get("ios_app_url"),
                "minis_type": parse_int(entry.get("minis_type")),
                "developer_id": str(developer["id"]) if developer.get("id") else None,
                "developer_name": developer.get("name"),
                "developer_area_cc": dev_area.get("cc"),
                "raw": entry,
            }
        )
    return out


def upsert_advertisers(session: Session, material_id: str, advertisers: Iterable[Dict[str, Any]]) -> int:
    """Upsert advertiser rows and link them to the material. Returns count."""
    now = datetime.now(timezone.utc)
    count = 0
    for adv in advertisers:
        session.execute(
            _UPSERT_ADVERTISER,
            {
                "id": adv["id"],
                "kind": adv.get("kind"),
                "type": adv.get("type"),
                "name": adv.get("name"),
                "icon": adv.get("icon"),
                "types": adv.get("types"),
                "alias": adv.get("alias"),
                "gp_app_url": adv.get("gp_app_url"),
                "ios_app_url": adv.get("ios_app_url"),
                "minis_type": adv.get("minis_type"),
                "developer_id": adv.get("developer_id"),
                "developer_name": adv.get("developer_name"),
                "developer_area_cc": adv.get("developer_area_cc"),
                "raw": json.dumps(adv.get("raw"), ensure_ascii=False) if adv.get("raw") is not None else None,
                "now": now,
            },
        )
        session.execute(_INSERT_EDGE, {"material_id": material_id, "advertiser_id": adv["id"]})
        count += 1
    return count
