"""MCP server for the ad_scraper (Ad Intelligence) service.

Exposes the competitor-ad library to AI agents (Claude Desktop, Claude
Code, the remake pipeline's future agents) so they can search the 8k+
mirrored creatives, trigger new scrapes, and hand a winning ad straight
to a content_pipeline project for a remake — without bespoke HTTP glue.

Mirrors the ig_scraper MCP design:
- FastMCP imported lazily; the API boots cleanly without the `mcp`
  package (mounting is skipped).
- Tools are thin adapters over `app.services.*` — no business logic
  here, so the service layer's unit tests cover the MCP surface too.
- Credential management is deliberately NOT exposed (REST-only) — too
  destructive behind an agent.
- Reachable only on the internal network (the API container exposes no
  host port in prod; dev binds 127.0.0.1).
"""

from typing import Any, Dict, List, Optional

from app.core.config import settings
from app.core.logging import logger


def _build_server():
    """Construct the FastMCP instance with every tool registered.

    Returns None when the `mcp` package isn't importable so callers can
    skip mounting cleanly.
    """
    try:
        # mcp 2.x renamed FastMCP → MCPServer; the decorator/transport API
        # we use (tool(), streamable_http_app()) is unchanged.
        try:
            from mcp.server.mcpserver import MCPServer as FastMCP  # type: ignore
        except ImportError:
            from mcp.server.fastmcp import FastMCP  # type: ignore
    except Exception as exc:  # noqa: BLE001
        logger.warning("mcp_unavailable", error=str(exc))
        return None

    mcp = FastMCP(
        name="ad-intelligence",
        instructions=(
            "Competitor ad intelligence: search 8k+ mirrored ad creatives "
            "(TikTok/Meta/etc. via AppGrowing), inspect one ad's full facts, "
            "get a playable media URL, enqueue new scrapes, and import a "
            "winning ad into a content_pipeline project to start a remake. "
            "Ranking signals: impressions (2y increment), run_days (days on "
            "air — proven ads run long), ad_count. Use list_filter_options "
            "first when composing scrape-job filters — invalid codes fail "
            "the whole upstream request."
        ),
    )

    # ------------------------------------------------------------------
    # Read tools
    # ------------------------------------------------------------------

    @mcp.tool()
    def search_ads(
        media: Optional[List[str]] = None,
        area: Optional[List[str]] = None,
        platform: Optional[List[str]] = None,
        material_type: Optional[int] = None,
        advertiser_id: Optional[str] = None,
        min_impressions: Optional[int] = None,
        min_run_days: Optional[int] = None,
        has_asr: Optional[bool] = None,
        mirrored_only: bool = True,
        active_since: Optional[str] = None,
        sort: str = "impressions_desc",
        limit: int = 20,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Search the mirrored competitor-ad library.

        Filters AND across kinds, OR within one: `media` = network codes
        ("13"=TikTok, "2"=Facebook, "1"=Instagram), `area` = ISO-2 country
        codes ("TR"), `platform` = OS ("1"=Android, "2"=iOS),
        `material_type` 201/202 = video, `active_since` = "YYYY-MM-DD"
        (still on air since). `sort` ∈ impressions_desc | end_date_desc |
        run_days_desc | first_seen_desc | duration_desc. `mirrored_only`
        keeps ads whose video is durably in our S3 (required for remakes).
        Returns up to `limit` (max 100) rows with metrics + facet names.
        """
        from app.services import queries
        from app.services.database import session_scope

        with session_scope() as session:
            return queries.search_materials(
                session,
                media=media,
                area=area,
                platform=platform,
                material_type=material_type,
                advertiser_id=advertiser_id,
                min_impressions=min_impressions,
                min_run_days=min_run_days,
                has_asr=has_asr,
                mirrored_only=mirrored_only,
                active_since=active_since,
                sort=sort,
                limit=max(1, min(limit, 100)),
                offset=max(0, offset),
            )

    @mcp.tool()
    def get_ad(material_id: str) -> Optional[Dict[str, Any]]:
        """Full record for one ad creative: slogan (the ad copy), ASR
        transcript when present, metrics, on-air window, resources,
        every facet (networks, countries, OS), and its advertisers."""
        from app.services import queries
        from app.services.database import session_scope

        with session_scope() as session:
            return queries.get_material(session, material_id)

    @mcp.tool()
    def get_ad_media_url(material_id: str, kind: str = "media", ttl: int = 3600) -> Dict[str, Any]:
        """Presigned URL for the mirrored creative file (`kind="media"`,
        usually the mp4) or its poster (`kind="poster"`). Errors when the
        ad was never mirrored — its CDN URL is likely expired too."""
        from app.core import s3
        from app.services import queries
        from app.services.database import session_scope

        with session_scope() as session:
            row = queries.get_material(session, material_id)
        if row is None:
            return {"error": "material not found"}
        key = row.get("media_s3_key") if kind == "media" else row.get("poster_s3_key")
        if not key:
            return {
                "error": f"no mirrored {kind} for this creative",
                "media_url_expires_at": str(row.get("media_url_expires_at")),
            }
        ttl = max(60, min(ttl, 86400))
        return {"material_id": material_id, "kind": kind, "url": s3.presigned_get_url(key, ttl=ttl), "expires_in": ttl}

    @mcp.tool()
    def list_advertisers(
        search: Optional[str] = None, kind: Optional[str] = None, limit: int = 20
    ) -> List[Dict[str, Any]]:
        """Competitor advertisers, busiest first (by creative count).
        `search` matches name or localized alias; `kind` ∈ App | AppBrand |
        Website | Playlet | Novel. Feed an advertiser's `id` back into
        search_ads(advertiser_id=...) to list their creatives."""
        from app.services import queries
        from app.services.database import session_scope

        with session_scope() as session:
            return queries.list_advertisers(
                session, search=search, kind=kind, limit=max(1, min(limit, 100)), offset=0
            )

    @mcp.tool()
    def list_filter_options() -> Dict[str, Any]:
        """The full upstream filter vocabulary (network/OS/format codes,
        sort orders, languages, known trap parameters). Consult this
        BEFORE composing enqueue_scrape_job filters — one invalid media
        code fails the whole upstream request."""
        from app.services import filter_schema
        from app.services.database import session_scope

        with session_scope() as session:
            return filter_schema.build(session)

    # ------------------------------------------------------------------
    # Write tools
    # ------------------------------------------------------------------

    @mcp.tool()
    def enqueue_scrape_job(
        filters: Dict[str, Any],
        page_from: int = 1,
        page_to: Optional[int] = None,
        order: str = "max_dt_desc",
        mirror: Optional[bool] = None,
        app_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Queue an ingestion job against AppGrowing. `filters` are
        materialList variables verbatim (media, area, platform, keyword,
        startDate/endDate...); omit `page`/`order` from filters. Hard
        ceiling 10 000 rows per filter set — partition broad pulls by
        date window / area / network. `mirror` downloads the creatives
        into our S3 (defaults to the service policy, normally on)."""
        from app.schemas.jobs import JobCreate
        from app.services import jobs as jobs_svc
        from app.services.database import session_scope

        # Reuse the REST schema so its guardrails apply here too: trap
        # params rejected, area format enforced, and `app_id` folded into
        # filters.searchDsl by the model validator.
        payload = JobCreate(
            filters=filters, page_from=page_from, page_to=page_to,
            order=order, mirror=mirror, app_id=app_id,
        )
        with session_scope() as session:
            job = jobs_svc.create_job(
                session,
                filters=payload.filters,
                page_from=payload.page_from,
                page_to=payload.page_to,
                order=payload.order,
                mirror=payload.mirror,
                max_attempts=payload.max_attempts,
            )
            return {"job_id": str(job.id), "status": job.status, "page_from": job.page_from, "page_to": job.page_to}

    @mcp.tool()
    def get_job_status(job_id: str) -> Optional[Dict[str, Any]]:
        """One scrape job's status + ingest stats (new/updated/mirrored
        counts, truncation flag)."""
        import uuid as _uuid

        from app.services import jobs as jobs_svc
        from app.services.database import session_scope

        try:
            jid = _uuid.UUID(job_id)
        except ValueError:
            return {"error": "job_id is not a uuid"}
        with session_scope() as session:
            job = jobs_svc.get_job(session, jid)
            if job is None:
                return None
            return {
                "job_id": str(job.id), "status": job.status, "attempt": job.attempt,
                "error": job.error, "stats": job.stats,
                "created_at": str(job.created_at), "finished_at": str(job.finished_at),
            }

    @mcp.tool()
    def annotate_ad(
        material_id: str,
        script: Optional[str] = None,
        summary: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Store your analysis of one creative: `script` = the video's
        scenario (shot-by-shot: what happens, who's on screen, on-screen
        text, hook/CTA), `summary` = a 1-3 sentence abstract. Watch the
        video via get_ad_media_url first. Only the fields you pass
        change; an empty string clears a field. These annotations are
        returned by get_ad (script+summary) and search_ads (summary), so
        the library becomes searchable by what ads actually do."""
        from app.services import annotations
        from app.services.database import session_scope

        with session_scope() as session:
            result = annotations.annotate(
                session, material_id, script=script, summary=summary
            )
        return result if result is not None else {"error": "material not found"}

    def _cp_request(method: str, path: str, *, json_body: Optional[dict] = None,
                    params: Optional[dict] = None, timeout: int = 60):
        """One call into content_pipeline with the service key. Returns
        (payload, error) — exactly one is non-None."""
        import httpx

        if not settings.CP_API_KEY:
            return None, "CP_API_KEY is not configured on ad_scraper"
        url = f"{settings.CP_API_URL.rstrip('/')}/api/v1{path}"
        try:
            resp = httpx.request(
                method, url, json=json_body, params=params,
                headers={"X-API-Key": settings.CP_API_KEY}, timeout=timeout,
            )
        except httpx.HTTPError as exc:
            return None, f"content_pipeline unreachable: {exc}"
        if resp.status_code >= 400:
            return None, f"content_pipeline {resp.status_code}: {resp.text[:300]}"
        return resp.json(), None

    @mcp.tool()
    def import_ad_to_project(
        material_id: str,
        project_id: str,
        auto_approve: bool = False,
        title: Optional[str] = None,
    ) -> Dict[str, Any]:
        """MARK AN AD AS A REFERENCE: hand one mirrored ad to a
        content_pipeline project as a reference video — the input a
        remake (or an external production) starts from. Copies the
        mirrored media into the project's prefix and carries
        slogan/ASR/metrics. `title` names the reference for humans
        ("Rakip X — before/after hook"); rename later with
        rename_reference. Returns the created reference; its `id` is
        what list_references shows and what upload_produced_video and
        the remake UI take."""
        payload, err = _cp_request(
            "POST", f"/projects/{project_id}/references/import-from-ads",
            json_body={
                "material_id": material_id, "auto_approve": auto_approve,
                "copy_media": True, "title": title,
            },
        )
        return payload if err is None else {"error": err}

    @mcp.tool()
    def rename_reference(project_id: str, reference_id: str, title: str) -> Dict[str, Any]:
        """Give a reference video a human name (max 255 chars). Shown in
        list_references and the panel; the source's own ad copy stays in
        `caption`."""
        payload, err = _cp_request(
            "PATCH", f"/projects/{project_id}/references/{reference_id}",
            json_body={"title": title.strip()[:255]},
        )
        if err is not None:
            return {"error": err}
        return {"reference_id": payload.get("id"), "title": payload.get("title")}

    @mcp.tool()
    def list_projects() -> List[Dict[str, Any]]:
        """The platform's projects (brands/tenants). Call this FIRST when
        the user names a project ("X projesine aktar") — every
        project-scoped tool (import_ad_to_project, list_references,
        upload_produced_video) takes the `id` from here. Reads the shared
        Postgres directly; archived projects are hidden."""
        from sqlalchemy import text as sql_text

        from app.services.database import session_scope

        with session_scope() as session:
            rows = session.execute(
                sql_text(
                    "SELECT id, slug, name, status FROM public.projects "
                    "WHERE status != 'archived' ORDER BY name"
                )
            ).mappings().all()
        return [
            {"id": str(r["id"]), "slug": r["slug"], "name": r["name"], "status": r["status"]}
            for r in rows
        ]

    @mcp.tool()
    def list_references(
        project_id: str,
        status: Optional[str] = None,
        limit: int = 25,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """List a project's reference videos (ads/reels already marked as
        references). `status` ∈ candidate | approved | archived. Rows
        carry a presigned `media_url` you can watch/download — the input
        for producing a video outside the pipeline — plus the reference's
        `title` (human name) and, when it came from Ad Intelligence, an
        `ad_stats` block (impressions, days on air, ad_count,
        advertisers) so you can pick the proven performers."""
        params: Dict[str, Any] = {"limit": max(1, min(limit, 100)), "offset": max(0, offset)}
        if status:
            params["status"] = status
        payload, err = _cp_request("GET", f"/projects/{project_id}/references", params=params)
        if err is not None:
            return [{"error": err}]
        rows = payload if isinstance(payload, list) else (payload or {}).get("items", [])
        out = []
        for r in rows:
            meta = r.get("metadata") or {}
            # Ad-Intelligence provenance → compact performance block.
            ad_stats = None
            if r.get("source_provider") == "appgrowing" or "impressions" in meta:
                ad_stats = {
                    "impressions": meta.get("impressions"),
                    "impressions_label": meta.get("impressions_label"),
                    "run_days": meta.get("run_days"),
                    "ad_count": meta.get("ad_count"),
                    "advertisers": meta.get("advertisers"),
                    "first_on_air": meta.get("first_on_air"),
                    "last_on_air": meta.get("last_on_air"),
                }
            out.append({
                "reference_id": r.get("id"),
                "title": r.get("title"),
                "source_provider": r.get("source_provider"),
                "source_external_id": r.get("source_external_id"),
                "caption": (r.get("caption") or "")[:160],
                "status": r.get("status"),
                "has_media": bool(r.get("media_s3_key")),
                "media_url": r.get("media_url"),
                "ad_stats": ad_stats,
                "imported_at": r.get("imported_at"),
            })
        return out

    @mcp.tool()
    def request_video_upload(project_id: str, filename: str = "produced.mp4") -> Dict[str, Any]:
        """Get a presigned PUT URL for uploading a LOCAL video file into
        the platform's storage. Flow for a file on your machine:
          1. Call this → {upload_url, s3_key}
          2. PUT the file:  curl -X PUT --upload-file video.mp4
             -H 'Content-Type: video/mp4' '<upload_url>'
          3. Call upload_produced_video(..., s3_key=<s3_key>) to register
             it against a reference.
        For a file already hosted at a URL, skip this and pass video_url
        to upload_produced_video directly."""
        payload, err = _cp_request(
            "POST", f"/projects/{project_id}/assets/upload-url",
            json_body={"kind": "misc", "filename": filename, "content_type": "video/mp4"},
        )
        if err is not None:
            return {"error": err}
        return {
            "upload_url": payload.get("upload_url"),
            "s3_key": payload.get("s3_key"),
            "headers": payload.get("headers") or {"Content-Type": "video/mp4"},
            "note": "PUT the file to upload_url, then call upload_produced_video with this s3_key.",
        }

    @mcp.tool()
    def upload_produced_video(
        project_id: str,
        reference_id: str,
        video_url: Optional[str] = None,
        s3_key: Optional[str] = None,
        caption: Optional[str] = None,
        cost_usd: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Register a FINISHED video produced outside the pipeline for
        one reference. `cost_usd` (optional) is what the production cost
        in USD — it shows up in the platform's cost reporting.
        Pass EXACTLY ONE of:
          - `video_url` — a URL the platform can fetch (public or
            presigned GET); the file is streamed in.
          - `s3_key` — from request_video_upload after you PUT the local
            file; registered with a server-side copy (no re-transfer).
        Lands as a remake in `final_review` — a human approves it in the
        panel, after which it enters the stock/publish flow. Returns the
        created remake (id + status)."""
        if bool(video_url) == bool(s3_key):
            return {"error": "pass exactly one of video_url or s3_key"}
        payload, err = _cp_request(
            "POST", f"/projects/{project_id}/remakes/import-external",
            json_body={
                "reference_id": reference_id, "video_url": video_url,
                "s3_key": s3_key, "caption": caption, "cost_usd": cost_usd,
            },
            timeout=330,  # the platform may stream the file inside this call
        )
        return payload if err is None else {"error": err}

    @mcp.tool()
    def list_remakes(
        project_id: str,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """List a project's remakes (newest first). `status` filters by
        pipeline state: analyzing | plan_review | rendering |
        needs_attention | final_review | done | rejected | archived.
        Rows carry id, reference_id, reference_title, status, costs and
        timestamps — use get_remake for the full detail."""
        payload, err = _cp_request(
            "GET", f"/projects/{project_id}/remakes",
            params={k: v for k, v in {"status": status, "limit": limit, "offset": offset}.items() if v is not None},
        )
        if err is not None:
            return [{"error": err}]
        rows = payload if isinstance(payload, list) else []
        # Trim the heavy plan_json from list rows — get_remake has it.
        for r in rows:
            r.pop("plan_json", None)
        return rows

    @mcp.tool()
    def get_remake(project_id: str, remake_id: str) -> Dict[str, Any]:
        """Full remake detail: status, shots, steps, progress, costs,
        reject reason (`error`), plus presigned `final_url` (the
        composed/uploaded video) and `source_url` (the reference video)
        valid ~1h."""
        payload, err = _cp_request("GET", f"/projects/{project_id}/remakes/{remake_id}")
        if err is not None:
            return {"error": err}
        # The per-second thumbnail strips are for the panel timeline —
        # dozens of URLs of noise in an agent context.
        payload.pop("filmstrip", None)
        return payload

    @mcp.tool()
    def approve_remake_final(project_id: str, remake_id: str) -> Dict[str, Any]:
        """Approve a remake at Gate 2 (final_review → done). This is a
        human approval gate — only call it when the operator explicitly
        asked for the approval."""
        payload, err = _cp_request(
            "POST", f"/projects/{project_id}/remakes/{remake_id}/approve-final",
            json_body={},
        )
        return payload if err is None else {"error": err}

    @mcp.tool()
    def reject_remake_final(
        project_id: str, remake_id: str, reason: Optional[str] = None
    ) -> Dict[str, Any]:
        """Reject a remake at Gate 2 (final_review → rejected). The
        optional `reason` is stored and shown on the remake. Reversible
        via reopen_remake. Only call on the operator's explicit ask."""
        payload, err = _cp_request(
            "POST", f"/projects/{project_id}/remakes/{remake_id}/reject-final",
            json_body={"reason": reason},
        )
        return payload if err is None else {"error": err}

    @mcp.tool()
    def reopen_remake(project_id: str, remake_id: str) -> Dict[str, Any]:
        """Bring a rejected remake back to Gate 2 (rejected →
        final_review) — e.g. after fixing the issue in `error`."""
        payload, err = _cp_request(
            "POST", f"/projects/{project_id}/remakes/{remake_id}/reopen",
        )
        if err is not None:
            return {"error": err}
        payload.pop("filmstrip", None)
        return payload

    @mcp.tool()
    def retry_remake(project_id: str, remake_id: str) -> Dict[str, Any]:
        """Recover a needs_attention remake: resets every failed step
        (shot-scoped and global) and resumes the pipeline."""
        payload, err = _cp_request(
            "POST", f"/projects/{project_id}/remakes/{remake_id}/retry",
        )
        if err is not None:
            return {"error": err}
        payload.pop("filmstrip", None)
        return payload

    return mcp


mcp_server = _build_server()
