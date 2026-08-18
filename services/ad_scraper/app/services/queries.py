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
    return [dict(row) for row in rows]


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
