"""Read-only access to the `ad_scraper` schema.

Sibling of `scraper_bridge.py`, same reasoning: `ad_scraper` shares this
Postgres instance (`ad_*` tables in the public schema) but depending on its
SQLModel classes would couple our deploy lifecycle to theirs. So we issue
raw SELECTs through the read engine and return plain dicts.

One thing is materially easier than the Instagram path: ad_scraper already
mirrors every creative into the shared S3 bucket at ingestion time, because
YouCloud's signed CDN URLs expire ~15 days out. So an import here does not
need to download anything — the bytes are already ours, and we only have to
decide whether to reference or copy the object (see
`app/services/references.py::import_from_ads`).
"""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import text

from app.core.logging import logger
from app.services.database import read_session_scope

_FETCH_MATERIAL = text(
    """
    SELECT
        m.id                    AS source_external_id,
        m.type                  AS material_type,
        m.creative_type,
        m.slogan,
        m.description,
        m.asr,
        m.txt_url,
        m.start_date,
        m.end_date,
        -- `run_days` counts DAYS ON AIR, not seconds. The API calls it
        -- `duration`, which is why ad_scraper renamed it.
        m.run_days,
        m.ad_count,
        m.similar_cnt,
        m.impression_inc_2y,
        m.impression_inc_2y_raw,
        m.gender,
        m.violation,
        m.media_format,
        m.media_width,
        m.media_height,
        -- Video length in seconds, from the creative's resource entry.
        m.media_duration_sec,
        m.media_url,
        m.poster_url,
        m.media_url_expires_at,
        m.media_s3_key,
        m.poster_s3_key,
        m.media_mirrored_at,
        m.first_seen_at,
        m.last_seen_at,
        -- Facets and advertisers flattened to name arrays. The panel wants
        -- labels, not codes, and aggregating here saves two round-trips.
        (
            SELECT array_agg(DISTINCT COALESCE(d.name, md.code))
              FROM ad_material_dimensions md
              LEFT JOIN ad_dimensions d ON d.kind = md.kind AND d.code = md.code
             WHERE md.material_id = m.id AND md.kind = 'media'
        ) AS media_names,
        (
            SELECT array_agg(DISTINCT md.code)
              FROM ad_material_dimensions md
             WHERE md.material_id = m.id AND md.kind = 'area'
        ) AS area_codes,
        (
            SELECT array_agg(DISTINCT COALESCE(d.name, md.code))
              FROM ad_material_dimensions md
              LEFT JOIN ad_dimensions d ON d.kind = md.kind AND d.code = md.code
             WHERE md.material_id = m.id AND md.kind = 'platform'
        ) AS platform_names,
        (
            SELECT array_agg(DISTINCT a.name)
              FROM ad_material_advertisers ma
              JOIN ad_advertisers a ON a.id = ma.advertiser_id
             WHERE ma.material_id = m.id AND a.name IS NOT NULL
        ) AS advertiser_names
    FROM ad_materials m
    WHERE m.id = :material_id
    LIMIT 1
    """
)


def fetch_ad_material(material_id: str) -> Optional[dict[str, Any]]:
    """Pull one `ad_materials` row plus its flattened facets.

    Returns None when the id isn't in ad_scraper's DB — most often because
    the operator pasted an id that was never ingested, or because the
    `ad_*` tables don't exist yet in this environment.
    """
    if not material_id or not isinstance(material_id, str):
        return None
    try:
        with read_session_scope() as session:
            row = session.exec(_FETCH_MATERIAL.bindparams(material_id=material_id)).mappings().first()
    except Exception as exc:  # noqa: BLE001 — a missing table must not 500 the API
        logger.warning("ad_scraper_bridge_fetch_failed", material_id=material_id[:64], error=str(exc))
        return None
    return dict(row) if row else None
