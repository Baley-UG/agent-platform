"""Read-side queries for the API.

Raw SQL rather than ORM expressions, for the same reason
content_pipeline's `scraper_bridge` does it: the facet filters compose
into a variable number of joins, and hand-written SQL makes the resulting
plan obvious. Everything routes through the read engine, so a configured
replica absorbs the analytical load.

Facet filtering is the one thing worth reading closely. Because facets
live in one generic edge table, each requested facet becomes its own join
with its own alias — `media=2&area=TR` means "has BOTH", not "has either".
Multiple values within a single facet are an `IN` (i.e. "either"), which
matches how the source UI behaves.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import text as sql_text
from sqlmodel import Session

# Whitelisted sorts. A user-supplied ORDER BY fragment would be an
# injection vector, so the API maps a short key onto one of these.
SORT_OPTIONS: Dict[str, str] = {
    "impressions_desc": "m.impression_inc_2y DESC NULLS LAST, m.id",
    "end_date_desc": "m.end_date DESC NULLS LAST, m.id",
    "run_days_desc": "m.run_days DESC NULLS LAST, m.id",
    "first_seen_desc": "m.first_seen_at DESC, m.id",
    "duration_desc": "m.media_duration_sec DESC NULLS LAST, m.id",
}
DEFAULT_SORT = "impressions_desc"

_MATERIAL_COLUMNS = """
    m.id, m.type, m.creative_type, m.start_date, m.end_date, m.run_days,
    m.ad_count, m.similar_cnt, m.impression_inc_2y_raw, m.impression_inc_2y,
    m.gender, m.slogan, m.description, m.txt_url,
    m.media_format, m.media_width, m.media_height, m.media_duration_sec,
    m.media_url, m.poster_url, m.media_url_expires_at,
    m.media_s3_key, m.poster_s3_key, m.media_mirrored_at,
    m.first_seen_at, m.last_seen_at
"""


def _facet_joins(facets: Sequence[Tuple[str, Sequence[str]]]) -> Tuple[str, Dict[str, Any]]:
    """Build one join per requested facet. Returns `(sql, params)`."""
    clauses: List[str] = []
    params: Dict[str, Any] = {}
    for position, (kind, codes) in enumerate(facets):
        if not codes:
            continue
        alias = f"d{position}"
        kind_key = f"{alias}_kind"
        codes_key = f"{alias}_codes"
        clauses.append(
            f" JOIN ad_material_dimensions {alias} "
            f"ON {alias}.material_id = m.id "
            f"AND {alias}.kind = :{kind_key} "
            f"AND {alias}.code = ANY(:{codes_key})"
        )
        params[kind_key] = kind
        params[codes_key] = list(codes)
    return "".join(clauses), params


# Facets attached to every row of a LIST response. `area` is deliberately
# absent: measured over 1 923 materials it averages 56.1 edges and peaks at
# 136, so putting it on 50 rows would mean ~2 800 entries in one payload for
# a column no table can render anyway. The four kept here average 9.1 edges
# per material combined. Full facets, `area` included, live on
# `GET /materials/{id}`.
_LIST_FACET_KINDS = ("media", "platform", "channel", "format")

# One creative can carry up to 60 advertisers (measured max; the upstream
# docs claim 66). A table shows a handful and a count, so cap the list and
# report the true total rather than silently truncating.
_LIST_ADVERTISER_CAP = 5


def _attach_list_facets(session: Session, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Add network/platform facets and advertisers to list rows.

    Two extra queries for the whole page, not per row — the point is that a
    list response without these is unusable for a table: `media_format` is
    the file container (`mp4`), so nothing in the bare row says whether the
    creative ran on TikTok or Facebook.
    """
    if not rows:
        return rows

    ids = [row["id"] for row in rows]
    buckets: Dict[str, Dict[str, List[Dict[str, Any]]]] = {
        material_id: {kind: [] for kind in _LIST_FACET_KINDS} for material_id in ids
    }

    facet_rows = (
        session.execute(
            sql_text("""
                SELECT md.material_id, md.kind, md.code, d.name
                  FROM ad_material_dimensions md
                  LEFT JOIN ad_dimensions d ON d.kind = md.kind AND d.code = md.code
                 WHERE md.material_id = ANY(:ids) AND md.kind = ANY(:kinds)
                 ORDER BY md.kind, d.name NULLS LAST, md.code
                """),
            {"ids": ids, "kinds": list(_LIST_FACET_KINDS)},
        )
        .mappings()
        .all()
    )
    for facet in facet_rows:
        bucket = buckets.get(facet["material_id"])
        if bucket is not None:
            bucket[facet["kind"]].append({"code": facet["code"], "name": facet["name"]})

    advertisers: Dict[str, List[Dict[str, Any]]] = {material_id: [] for material_id in ids}
    totals: Dict[str, int] = {material_id: 0 for material_id in ids}
    adv_rows = (
        session.execute(
            sql_text("""
                SELECT material_id, advertiser_id, name, kind, total
                  FROM (
                        SELECT ma.material_id,
                               ma.advertiser_id,
                               a.name,
                               a.kind,
                               ROW_NUMBER() OVER (PARTITION BY ma.material_id
                                                  ORDER BY a.name NULLS LAST, ma.advertiser_id) AS rn,
                               COUNT(*) OVER (PARTITION BY ma.material_id) AS total
                          FROM ad_material_advertisers ma
                          LEFT JOIN ad_advertisers a ON a.id = ma.advertiser_id
                         WHERE ma.material_id = ANY(:ids)
                       ) ranked
                 WHERE rn <= :cap
                 ORDER BY material_id, rn
                """),
            {"ids": ids, "cap": _LIST_ADVERTISER_CAP},
        )
        .mappings()
        .all()
    )
    for adv in adv_rows:
        material_id = adv["material_id"]
        if material_id in advertisers:
            advertisers[material_id].append({"id": adv["advertiser_id"], "name": adv["name"], "kind": adv["kind"]})
            totals[material_id] = adv["total"]

    for row in rows:
        material_id = row["id"]
        row.update(buckets[material_id])
        row["advertisers"] = advertisers[material_id]
        row["advertiser_count"] = totals[material_id]
    return rows


def search_materials(
    session: Session,
    *,
    media: Optional[Sequence[str]] = None,
    area: Optional[Sequence[str]] = None,
    platform: Optional[Sequence[str]] = None,
    channel: Optional[Sequence[str]] = None,
    format_: Optional[Sequence[str]] = None,
    material_type: Optional[int] = None,
    advertiser_id: Optional[str] = None,
    min_impressions: Optional[int] = None,
    min_run_days: Optional[int] = None,
    has_asr: Optional[bool] = None,
    mirrored_only: bool = False,
    active_since: Optional[str] = None,
    sort: str = DEFAULT_SORT,
    limit: int = 50,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    """List materials matching the given filters."""
    order_by = SORT_OPTIONS.get(sort, SORT_OPTIONS[DEFAULT_SORT])

    join_sql, params = _facet_joins(
        [
            ("media", media or []),
            ("area", area or []),
            ("platform", platform or []),
            ("channel", channel or []),
            ("format", format_ or []),
        ]
    )

    where: List[str] = ["1=1"]
    if advertiser_id:
        join_sql += " JOIN ad_material_advertisers ma ON ma.material_id = m.id AND ma.advertiser_id = :advertiser_id"
        params["advertiser_id"] = advertiser_id
    if material_type is not None:
        where.append("m.type = :material_type")
        params["material_type"] = material_type
    if min_impressions is not None:
        where.append("m.impression_inc_2y >= :min_impressions")
        params["min_impressions"] = min_impressions
    if min_run_days is not None:
        where.append("m.run_days >= :min_run_days")
        params["min_run_days"] = min_run_days
    if has_asr is True:
        where.append("m.asr IS NOT NULL AND m.asr <> ''")
    elif has_asr is False:
        where.append("(m.asr IS NULL OR m.asr = '')")
    if mirrored_only:
        where.append("m.media_s3_key IS NOT NULL")
    if active_since:
        where.append("m.end_date >= CAST(:active_since AS date)")
        params["active_since"] = active_since

    params["limit"] = limit
    params["offset"] = offset

    statement = sql_text(f"""
        SELECT {_MATERIAL_COLUMNS}
          FROM ad_materials m
          {join_sql}
         WHERE {" AND ".join(where)}
         ORDER BY {order_by}
         LIMIT :limit OFFSET :offset
        """)
    rows = session.execute(statement, params).mappings().all()
    return _attach_list_facets(session, [dict(row) for row in rows])


def get_material(session: Session, material_id: str) -> Optional[Dict[str, Any]]:
    """Fetch one material with its resources, facets and advertisers."""
    row = (
        session.execute(
            sql_text(f"SELECT {_MATERIAL_COLUMNS}, m.asr, m.violation FROM ad_materials m WHERE m.id = :id"),
            {"id": material_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        return None

    material = dict(row)
    material["resources"] = [
        dict(r)
        for r in session.execute(
            sql_text("""
                SELECT idx, resource_id, format, width, height, duration_sec,
                       path, poster, url_expires_at, s3_key
                  FROM ad_material_resources
                 WHERE material_id = :id
                 ORDER BY idx
                """),
            {"id": material_id},
        )
        .mappings()
        .all()
    ]
    material["dimensions"] = [
        dict(r)
        for r in session.execute(
            sql_text("""
                SELECT md.kind, md.code, d.name, d.icon, d.parent_code
                  FROM ad_material_dimensions md
                  LEFT JOIN ad_dimensions d ON d.kind = md.kind AND d.code = md.code
                 WHERE md.material_id = :id
                 ORDER BY md.kind, md.code
                """),
            {"id": material_id},
        )
        .mappings()
        .all()
    ]
    material["advertisers"] = [
        dict(r)
        for r in session.execute(
            sql_text("""
                SELECT a.id, a.kind, a.type, a.name, a.icon, a.alias,
                       a.gp_app_url, a.ios_app_url,
                       a.developer_id, a.developer_name, a.developer_area_cc
                  FROM ad_material_advertisers ma
                  JOIN ad_advertisers a ON a.id = ma.advertiser_id
                 WHERE ma.material_id = :id
                 ORDER BY a.name NULLS LAST, a.id
                """),
            {"id": material_id},
        )
        .mappings()
        .all()
    ]
    return material


def list_advertisers(
    session: Session,
    *,
    kind: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    """List advertisers with a creative count, busiest first."""
    where: List[str] = ["1=1"]
    params: Dict[str, Any] = {"limit": limit, "offset": offset}
    if kind:
        where.append("a.kind = :kind")
        params["kind"] = kind
    if search:
        where.append("(a.name ILIKE :search OR a.alias ILIKE :search)")
        params["search"] = f"%{search}%"

    rows = (
        session.execute(
            sql_text(f"""
            SELECT a.id, a.kind, a.type, a.name, a.icon, a.alias,
                   a.gp_app_url, a.ios_app_url,
                   a.developer_id, a.developer_name, a.developer_area_cc,
                   COUNT(ma.material_id) AS material_count
              FROM ad_advertisers a
              LEFT JOIN ad_material_advertisers ma ON ma.advertiser_id = a.id
             WHERE {" AND ".join(where)}
             GROUP BY a.id
             ORDER BY material_count DESC, a.name NULLS LAST
             LIMIT :limit OFFSET :offset
            """),
            params,
        )
        .mappings()
        .all()
    )
    return [dict(row) for row in rows]


def list_dimensions(
    session: Session,
    *,
    kind: Optional[str] = None,
    limit: int = 500,
) -> List[Dict[str, Any]]:
    """List known facet values with usage counts — feeds the panel's filters."""
    where: List[str] = ["1=1"]
    params: Dict[str, Any] = {"limit": limit}
    if kind:
        where.append("d.kind = :kind")
        params["kind"] = kind

    rows = (
        session.execute(
            sql_text(f"""
            SELECT d.kind, d.code, d.name, d.icon, d.description, d.parent_code,
                   COUNT(md.material_id) AS material_count
              FROM ad_dimensions d
              LEFT JOIN ad_material_dimensions md ON md.kind = d.kind AND md.code = d.code
             WHERE {" AND ".join(where)}
             GROUP BY d.kind, d.code
             ORDER BY d.kind, material_count DESC, d.name NULLS LAST
             LIMIT :limit
            """),
            params,
        )
        .mappings()
        .all()
    )
    return [dict(row) for row in rows]
