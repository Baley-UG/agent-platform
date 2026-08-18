"""Upsert helper for `ad_materials` (+ resources, dimensions, advertisers).

This is the load-bearing write path. One `upsert_material` call:

* upserts the material row, denormalising the primary resource onto it;
* writes every `creative.resource[]` entry to `ad_material_resources`;
* upserts the six facet arrays and their edges;
* upserts every `campaign[]` entity and its edge.

Update semantics are null-preserving (`COALESCE(EXCLUDED.x, existing.x)`)
for everything the API can omit on a later sighting, and last-writer-wins
for the metrics that legitimately change (`end_date`, `run_days`,
impressions, counts). The signed media URLs are always overwritten: a
fresh signature is strictly more useful than a stale one.

`raw` holds the untouched payload — the same rationale as `ig_posts.raw`.
When a column mapping improves, the fix is a JSONB backfill, not a
re-scrape of 10 000 rows.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import text as sql_text
from sqlmodel import Session

from app.services.parsing import expires_at_from_auth_key, parse_compact_number, parse_date, parse_int
from app.services.persistence.advertisers import extract_advertisers, upsert_advertisers
from app.services.persistence.dimensions import extract_dimensions, upsert_dimensions

# `xmax = 0` is true only for a freshly inserted tuple; an ON CONFLICT
# update leaves the old row's xmax set. It is the cheapest way to learn
# whether we created or refreshed a row without a prior SELECT.
_UPSERT_MATERIAL = sql_text("""
    INSERT INTO ad_materials (
        id, type, creative_type, start_date, end_date, run_days,
        ad_count, similar_cnt, impression_inc_2y_raw, impression_inc_2y,
        gender, violation, slogan, description, txt_url, asr,
        media_format, media_width, media_height, media_duration_sec,
        media_url, poster_url, media_url_expires_at,
        raw, discovered_via_job_id, first_seen_at, last_seen_at
    )
    VALUES (
        :id, :type, :creative_type, :start_date, :end_date, :run_days,
        :ad_count, :similar_cnt, :impression_inc_2y_raw, :impression_inc_2y,
        :gender, :violation, :slogan, :description, :txt_url, :asr,
        :media_format, :media_width, :media_height, :media_duration_sec,
        :media_url, :poster_url, :media_url_expires_at,
        CAST(:raw AS jsonb), :job_id, :now, :now
    )
    ON CONFLICT (id) DO UPDATE SET
        type                  = COALESCE(EXCLUDED.type, ad_materials.type),
        creative_type         = COALESCE(EXCLUDED.creative_type, ad_materials.creative_type),
        start_date            = COALESCE(EXCLUDED.start_date, ad_materials.start_date),
        -- These four legitimately move as a campaign keeps running.
        end_date              = COALESCE(EXCLUDED.end_date, ad_materials.end_date),
        run_days              = COALESCE(EXCLUDED.run_days, ad_materials.run_days),
        ad_count              = COALESCE(EXCLUDED.ad_count, ad_materials.ad_count),
        similar_cnt           = COALESCE(EXCLUDED.similar_cnt, ad_materials.similar_cnt),
        impression_inc_2y_raw = COALESCE(EXCLUDED.impression_inc_2y_raw, ad_materials.impression_inc_2y_raw),
        impression_inc_2y     = COALESCE(EXCLUDED.impression_inc_2y, ad_materials.impression_inc_2y),
        gender                = COALESCE(EXCLUDED.gender, ad_materials.gender),
        violation             = COALESCE(EXCLUDED.violation, ad_materials.violation),
        slogan                = COALESCE(EXCLUDED.slogan, ad_materials.slogan),
        description           = COALESCE(EXCLUDED.description, ad_materials.description),
        txt_url               = COALESCE(EXCLUDED.txt_url, ad_materials.txt_url),
        -- ASR is backfilled by the platform over time: a creative with no
        -- transcript today can have one next week, so never overwrite a
        -- populated value with an empty one.
        asr                   = COALESCE(NULLIF(EXCLUDED.asr, ''), ad_materials.asr),
        media_format          = COALESCE(EXCLUDED.media_format, ad_materials.media_format),
        media_width           = COALESCE(EXCLUDED.media_width, ad_materials.media_width),
        media_height          = COALESCE(EXCLUDED.media_height, ad_materials.media_height),
        media_duration_sec    = COALESCE(EXCLUDED.media_duration_sec, ad_materials.media_duration_sec),
        -- Signed URLs: always take the newer signature.
        media_url             = COALESCE(EXCLUDED.media_url, ad_materials.media_url),
        poster_url            = COALESCE(EXCLUDED.poster_url, ad_materials.poster_url),
        media_url_expires_at  = COALESCE(EXCLUDED.media_url_expires_at, ad_materials.media_url_expires_at),
        raw                   = COALESCE(EXCLUDED.raw, ad_materials.raw),
        last_seen_at          = EXCLUDED.last_seen_at
    RETURNING (xmax = 0) AS inserted, ad_materials.media_s3_key, ad_materials.poster_s3_key
    """)

_UPSERT_RESOURCE = sql_text("""
    INSERT INTO ad_material_resources (
        material_id, idx, resource_id, format, width, height,
        duration_sec, path, poster, url_expires_at
    )
    VALUES (
        :material_id, :idx, :resource_id, :format, :width, :height,
        :duration_sec, :path, :poster, :url_expires_at
    )
    ON CONFLICT (material_id, idx) DO UPDATE SET
        resource_id    = COALESCE(EXCLUDED.resource_id, ad_material_resources.resource_id),
        format         = COALESCE(EXCLUDED.format, ad_material_resources.format),
        width          = COALESCE(EXCLUDED.width, ad_material_resources.width),
        height         = COALESCE(EXCLUDED.height, ad_material_resources.height),
        duration_sec   = COALESCE(EXCLUDED.duration_sec, ad_material_resources.duration_sec),
        path           = COALESCE(EXCLUDED.path, ad_material_resources.path),
        poster         = COALESCE(EXCLUDED.poster, ad_material_resources.poster),
        url_expires_at = COALESCE(EXCLUDED.url_expires_at, ad_material_resources.url_expires_at)
    """)


def _jsonb(value: Any) -> Optional[str]:
    """Serialise for a `CAST(:x AS jsonb)` bind, or None."""
    if value is None:
        return None
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return None


def _violation_text(value: Any) -> Optional[str]:
    """Normalise `violation` to a text label.

    Observed as a plain string ("Human Exploitation") on the small share of
    creatives that carry one. A structured value would be a shape change, so
    we serialise it to JSON text rather than dropping it — the column stays
    readable and `raw` still holds the original.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    return _jsonb(value)


def extract_resources(material: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Flatten `creative.resource[]`. Pure, unit-testable.

    Note the units: `resource.duration` is the video length in SECONDS,
    unlike the material-level `duration`, which counts days on air.
    """
    creative = material.get("creative")
    if not isinstance(creative, dict):
        return []
    entries = creative.get("resource")
    if not isinstance(entries, list):
        return []

    out: List[Dict[str, Any]] = []
    for idx, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        path = entry.get("path")
        poster = entry.get("poster")
        out.append(
            {
                "idx": idx,
                "resource_id": str(entry["id"]) if entry.get("id") else None,
                "format": (entry.get("format") or None),
                "width": parse_int(entry.get("width")),
                "height": parse_int(entry.get("height")),
                "duration_sec": parse_int(entry.get("duration")),
                "path": path or None,
                "poster": poster or None,
                "url_expires_at": expires_at_from_auth_key(path),
            }
        )
    return out


def build_material_params(
    material: Dict[str, Any],
    *,
    job_id: Optional[uuid.UUID],
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Map one API material onto the `ad_materials` bind parameters.

    Pure, so the mapping (and every unit quirk it papers over) can be
    tested against a captured payload without a database.
    """
    now = now or datetime.now(timezone.utc)
    creative = material.get("creative") if isinstance(material.get("creative"), dict) else {}
    resources = extract_resources(material)
    primary = resources[0] if resources else {}

    return {
        "id": str(material["id"]),
        "type": parse_int(material.get("type")),
        "creative_type": parse_int(creative.get("type")),
        "start_date": parse_date(material.get("startDate")),
        "end_date": parse_date(material.get("endDate")),
        # API `duration` = days on air. The video length lives on the resource.
        "run_days": parse_int(material.get("duration")),
        "ad_count": parse_int(material.get("cnt_ad_id")),
        "similar_cnt": parse_int(material.get("similar_cnt")),
        "impression_inc_2y_raw": (
            str(material["impression_inc_2y"]) if material.get("impression_inc_2y") is not None else None
        ),
        "impression_inc_2y": parse_compact_number(material.get("impression_inc_2y")),
        "gender": parse_int(material.get("gender")),
        "violation": _violation_text(material.get("violation")),
        "slogan": creative.get("slogan") or None,
        "description": creative.get("description") or None,
        "txt_url": creative.get("txtUrl") or None,
        "asr": material.get("asr") or None,
        "media_format": primary.get("format"),
        "media_width": primary.get("width"),
        "media_height": primary.get("height"),
        "media_duration_sec": primary.get("duration_sec"),
        "media_url": primary.get("path"),
        "poster_url": primary.get("poster"),
        "media_url_expires_at": primary.get("url_expires_at"),
        "raw": _jsonb(material),
        "job_id": job_id,
        "now": now,
    }


@dataclass
class UpsertResult:
    """Outcome of one `upsert_material` call."""

    material_id: str
    # "new" when the row was inserted, "updated" when it already existed.
    outcome: str
    resources: int = 0
    dimensions: int = 0
    advertisers: int = 0
    # Mirror keys the row ALREADY had. Lets the caller skip re-downloading
    # bytes we hold: a re-run of the same filter would otherwise re-fetch
    # every video it fetched last time.
    existing_media_key: Optional[str] = None
    existing_poster_key: Optional[str] = None

    @property
    def already_mirrored(self) -> bool:
        """True when this material's media is already in our bucket."""
        return bool(self.existing_media_key)


def upsert_material(
    session: Session,
    *,
    material: Dict[str, Any],
    job_id: Optional[uuid.UUID] = None,
) -> UpsertResult:
    """Upsert one material and everything hanging off it.

    Raises `KeyError` when the payload has no `id` — a material without
    identity cannot be deduped, and silently skipping it would make the
    job's counts lie.
    """
    if not material.get("id"):
        raise KeyError("material payload has no 'id'")

    now = datetime.now(timezone.utc)
    params = build_material_params(material, job_id=job_id, now=now)
    material_id = params["id"]

    row = session.execute(_UPSERT_MATERIAL, params).first()
    outcome = "new" if (row is not None and row[0]) else "updated"
    existing_media_key = row[1] if row is not None else None
    existing_poster_key = row[2] if row is not None else None

    resources = extract_resources(material)
    for resource in resources:
        session.execute(_UPSERT_RESOURCE, {"material_id": material_id, **resource})

    dimension_count = upsert_dimensions(session, material_id, extract_dimensions(material))
    advertiser_count = upsert_advertisers(session, material_id, extract_advertisers(material))

    return UpsertResult(
        material_id=material_id,
        outcome=outcome,
        resources=len(resources),
        dimensions=dimension_count,
        advertisers=advertiser_count,
        existing_media_key=existing_media_key,
        existing_poster_key=existing_poster_key,
    )
