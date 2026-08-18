"""Job runner — walks a filter set's page window and persists what it finds.

One `run_job` call is one `ad_scrape_jobs` row executed end to end:

    for page in page_from..page_to:
        fetch materialList
        for each material: upsert (own transaction) then mirror
    record stats

Two decisions worth knowing about:

**Per-material transactions.** Each material gets its own `session_scope`,
so one malformed payload cannot roll back the fifty siblings that already
persisted. Same trade-off ig_scraper made: more commits, slower, but a bad
row costs one row.

**Truncation is reported, never hidden.** The API caps `page` at 200 with
a server-fixed `limit` of 50 — a hard 10 000-row ceiling per filter set.
When `total` exceeds what the requested window can return, we set
`stats.truncated` and log `ad_filter_too_broad`. A silent cut would read
as "we ingested everything matching this filter", which would be false.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from app.core.config import settings
from app.core.logging import logger
from app.core.metrics import (
    ad_advertisers_saved_total,
    ad_filter_truncated_total,
    ad_materials_saved_total,
)
from app.services import credentials as creds
from app.services import mirror
from app.services.database import session_scope
from app.services.persistence.materials import upsert_material
from app.services.youcloud.client import YouCloudClient


@dataclass
class IngestStats:
    """Counters recorded onto `ad_scrape_jobs.stats`."""

    pages_fetched: int = 0
    materials_seen: int = 0
    materials_new: int = 0
    materials_updated: int = 0
    materials_skipped: int = 0
    # Fan-out volume: one creative can carry 66 advertisers and dozens of
    # facet edges, so row counts alone understate what a job wrote.
    advertiser_edges: int = 0
    dimension_edges: int = 0
    mirrored: int = 0
    mirror_failed: int = 0
    mirror_skipped: int = 0
    # Already in our bucket from an earlier run — no bytes fetched. A re-run
    # of the same filter used to re-download every video it already held.
    mirror_cached: int = 0
    total_reported: Optional[int] = None
    truncated: bool = False
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        """JSON-serialisable view for the job row."""
        return {
            "pages_fetched": self.pages_fetched,
            "materials_seen": self.materials_seen,
            "materials_new": self.materials_new,
            "materials_updated": self.materials_updated,
            "materials_skipped": self.materials_skipped,
            "advertiser_edges": self.advertiser_edges,
            "dimension_edges": self.dimension_edges,
            "mirrored": self.mirrored,
            "mirror_failed": self.mirror_failed,
            "mirror_skipped": self.mirror_skipped,
            "mirror_cached": self.mirror_cached,
            "total_reported": self.total_reported,
            "truncated": self.truncated,
            "notes": self.notes,
        }


def _note_truncation(stats: IngestStats, *, page_to: int, total: Optional[int]) -> None:
    """Flag a filter set whose result count exceeds what we can page through."""
    if total is None:
        return
    stats.total_reported = total
    reachable = page_to * settings.AD_PAGE_SIZE
    ceiling = settings.max_rows_per_filter_set
    if total <= reachable:
        return

    stats.truncated = True
    ad_filter_truncated_total.inc()
    if total > ceiling:
        note = (
            f"filter set reports {total} rows but the API can only ever return {ceiling} "
            f"(page <= {settings.AD_MAX_PAGE} x limit {settings.AD_PAGE_SIZE}). "
            "Partition the filter — date window, area, media, platform, keyword — and run several jobs."
        )
    else:
        note = (
            f"filter set reports {total} rows; this job's window covers {reachable}. "
            f"Raise page_to (max {settings.AD_MAX_PAGE}) to reach the rest."
        )
    if note not in stats.notes:
        stats.notes.append(note)
        logger.warning(
            "ad_filter_too_broad",
            total_reported=total,
            reachable=reachable,
            ceiling=ceiling,
            page_to=page_to,
        )


async def _persist_page(
    rows: list,
    *,
    job_id: uuid.UUID,
    job_mirror: Optional[bool],
    stats: IngestStats,
) -> None:
    """Upsert every material on one page, then mirror its media."""
    do_mirror = mirror.should_mirror(job_mirror=job_mirror)

    for row in rows:
        if not isinstance(row, dict):
            stats.materials_skipped += 1
            continue
        material = row.get("material")
        if not isinstance(material, dict) or not material.get("id"):
            stats.materials_skipped += 1
            continue

        stats.materials_seen += 1

        # Own transaction per material — a bad payload costs one row.
        try:
            with session_scope() as session:
                result = upsert_material(session, material=material, job_id=job_id)
        except Exception as exc:  # noqa: BLE001 — one bad row must not stop the page
            stats.materials_skipped += 1
            logger.warning(
                "ad_material_upsert_failed",
                material_id=str(material.get("id"))[:64],
                error=f"{type(exc).__name__}: {exc}",
            )
            continue

        material_id = result.material_id
        if result.outcome == "new":
            stats.materials_new += 1
        else:
            stats.materials_updated += 1
        stats.advertiser_edges += result.advertisers
        stats.dimension_edges += result.dimensions
        ad_materials_saved_total.labels(outcome=result.outcome).inc()
        if result.advertisers:
            ad_advertisers_saved_total.inc(result.advertisers)

        if not do_mirror:
            stats.mirror_skipped += 1
            continue

        # Already ours — skip the download. Re-running a filter is the normal
        # way to pick up newly-published creatives, and without this every
        # re-run re-fetches every video it fetched before.
        if result.already_mirrored:
            stats.mirror_cached += 1
            continue

        creative = material.get("creative") if isinstance(material.get("creative"), dict) else {}
        resources = creative.get("resource") if isinstance(creative.get("resource"), list) else []
        primary = resources[0] if resources and isinstance(resources[0], dict) else {}
        media_url = primary.get("path")
        poster_url = primary.get("poster")

        if not media_url and not poster_url:
            stats.mirror_skipped += 1
            continue

        media_key, poster_key = await mirror.transfer_async(
            material_id=material_id,
            media_url=media_url,
            poster_url=poster_url,
        )
        if media_key or poster_key:
            with session_scope() as session:
                mirror.persist_keys(
                    session,
                    material_id=material_id,
                    media_key=media_key,
                    poster_key=poster_key,
                )
            stats.mirrored += 1
        else:
            stats.mirror_failed += 1


async def run_job(
    *,
    job_id: uuid.UUID,
    filters: Dict[str, Any],
    page_from: int,
    page_to: int,
    order: str,
    job_mirror: Optional[bool],
) -> IngestStats:
    """Execute one ingestion job. Raises `YouCloudError` on API failure.

    The caller (the worker) maps those exceptions onto job state: retry for
    the transient ones, terminal for `AuthExpired` / `PlanDenied` /
    `BadFilter` — none of which another attempt can fix.
    """
    stats = IngestStats()

    async with YouCloudClient(session_provider=_session_provider) as client:
        async for page, payload in client.paginate_materials(
            filters,
            page_from=page_from,
            page_to=page_to,
            order=order,
        ):
            stats.pages_fetched += 1
            _note_truncation(stats, page_to=page_to, total=payload.get("total"))

            rows = payload.get("data")
            if not isinstance(rows, list) or not rows:
                logger.info("ad_page_empty", job_id=str(job_id), page=page)
                break

            await _persist_page(rows, job_id=job_id, job_mirror=job_mirror, stats=stats)
            logger.info(
                "ad_page_ingested",
                job_id=str(job_id),
                page=page,
                seen=stats.materials_seen,
                new=stats.materials_new,
                updated=stats.materials_updated,
            )

    # A page that returned rows proves the cookie works; record that so an
    # expiring-but-valid session doesn't look stale on the dashboard.
    if stats.pages_fetched:
        with session_scope() as session:
            creds.mark_ok(session)

    return stats


async def _session_provider() -> Optional[str]:
    """Hand the client the current session token.

    Thin indirection so the client never imports the credentials module —
    which keeps it constructible in tests with a plain lambda. Reads the
    in-process cache, so the common case costs no DB round-trip.
    """
    return creds.current_cookie()
