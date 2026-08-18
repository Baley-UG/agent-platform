"""Structural smoke tests — tables, routes, schema validation, crypto.

Cheap assertions that catch the mistakes which otherwise only show up at
container start: a table that never registered, a router that never
mounted, a validator that lets an impossible job through.
"""

import pytest
from sqlmodel import SQLModel

import app.models  # noqa: F401 — side-effect import registers the tables
from app.core.config import settings
from app.services import crypto

EXPECTED_TABLES = {
    "ad_materials",
    "ad_material_resources",
    "ad_dimensions",
    "ad_material_dimensions",
    "ad_advertisers",
    "ad_material_advertisers",
    "ad_scrape_jobs",
    "ad_credentials",
}


class TestImports:
    def test_api_app_imports(self):
        from app.main import app

        assert app.title == settings.PROJECT_NAME

    def test_worker_imports(self):
        import app.worker

        assert hasattr(app.worker, "main")


class TestTables:
    def test_all_tables_register(self):
        registered = set(SQLModel.metadata.tables)
        assert EXPECTED_TABLES <= registered, f"missing: {EXPECTED_TABLES - registered}"

    def test_no_unexpected_tables(self):
        ad_tables = {t for t in SQLModel.metadata.tables if t.startswith("ad_")}
        assert ad_tables == EXPECTED_TABLES

    def test_material_has_the_mirror_columns(self):
        columns = set(SQLModel.metadata.tables["ad_materials"].columns.keys())
        assert {"media_s3_key", "poster_s3_key", "media_mirrored_at", "media_url_expires_at"} <= columns

    def test_run_days_is_named_for_what_it_holds(self):
        """`ad_materials` must not carry the API's misleading `duration`."""
        columns = set(SQLModel.metadata.tables["ad_materials"].columns.keys())
        assert "run_days" in columns
        assert "duration" not in columns

    def test_dimension_edge_is_indexed_for_reverse_lookup(self):
        indexes = {i.name for i in SQLModel.metadata.tables["ad_material_dimensions"].indexes}
        assert "ix_ad_material_dimensions_kind_code" in indexes


class TestRoutes:
    """Asserted against the OpenAPI schema, not `app.routes`.

    FastAPI >= 0.141 wraps included routers in a lazy `_IncludedRouter`
    that matches requests but exposes no `.path`, so walking `app.routes`
    finds only the top-level ones. The schema is also what the main app's
    `openapi_federation` reads to publish this service's routes through the
    gateway, so it is the surface that actually has to be right.
    """

    @pytest.mark.parametrize(
        "expected",
        [
            "/health",
            "/ready",
            "/api/v1/credentials",
            "/api/v1/credentials/session",
            "/api/v1/credentials/session/invalidate-cache",
            "/api/v1/credentials/disable",
            "/api/v1/jobs",
            "/api/v1/jobs/{job_id}",
            "/api/v1/jobs/{job_id}/cancel",
            "/api/v1/jobs/{job_id}/retry",
            "/api/v1/materials",
            "/api/v1/materials/{material_id}",
            "/api/v1/materials/{material_id}/media-url",
            "/api/v1/advertisers",
            "/api/v1/advertisers/{advertiser_id}/materials",
            "/api/v1/dimensions",
        ],
    )
    def test_expected_routes_are_published(self, expected):
        from app.main import app

        assert expected in app.openapi()["paths"], f"route not published: {expected}"

    def test_metrics_endpoint_mounted(self):
        """Added via `add_route`, so it is a plain Route on `app.routes` and
        deliberately absent from the OpenAPI schema.
        """
        from app.main import app

        assert "/metrics" in {getattr(route, "path", "") for route in app.routes}

    def test_protected_routes_declare_the_api_key_scheme(self):
        from app.main import app

        schema = app.openapi()
        assert "ad_scraper_api_key" in schema["components"]["securitySchemes"]
        # A missing dependency on a write route would be a silent auth hole.
        assert schema["paths"]["/api/v1/jobs"]["post"].get("security")


class TestJobCreateValidation:
    def test_defaults_page_to_from_config(self):
        from app.schemas.jobs import JobCreate

        job = JobCreate(filters={"purpose": 2})
        assert job.page_to == settings.AD_DEFAULT_PAGE_TO

    def test_rejects_a_window_past_the_api_ceiling(self):
        from app.schemas.jobs import JobCreate

        with pytest.raises(ValueError) as info:
            JobCreate(filters={}, page_to=settings.AD_MAX_PAGE + 1)
        assert "narrow the filter" in str(info.value).lower()

    def test_accepts_the_last_valid_page(self):
        from app.schemas.jobs import JobCreate

        assert JobCreate(filters={}, page_to=settings.AD_MAX_PAGE).page_to == settings.AD_MAX_PAGE

    def test_rejects_inverted_window(self):
        from app.schemas.jobs import JobCreate

        with pytest.raises(ValueError):
            JobCreate(filters={}, page_from=10, page_to=2)

    @pytest.mark.parametrize("key", ["page", "order"])
    def test_rejects_worker_owned_keys_inside_filters(self, key):
        """Two sources of truth for the page number is a silent-wrong-answer
        bug, so the schema refuses it outright.
        """
        from app.schemas.jobs import JobCreate

        with pytest.raises(ValueError) as info:
            JobCreate(filters={"purpose": 2, key: 7})
        assert key in str(info.value)

    def test_app_id_compiles_into_search_dsl(self):
        """App-id filtering goes through `searchDsl`, mirroring the web UI's
        `advanced=[{"key":"appid",...}]` panel — verified against the live
        endpoint (6 559 rows for appid 1661308505)."""
        from app.schemas.jobs import JobCreate

        job = JobCreate(filters={"purpose": 2}, app_id="1661308505")
        assert job.filters["searchDsl"] == [{"key": "appid", "value": "1661308505", "type": "equal"}]

    def test_app_id_appends_to_an_existing_dsl(self):
        from app.schemas.jobs import JobCreate

        job = JobCreate(
            filters={"purpose": 2, "searchDsl": [{"key": "other", "value": "x", "type": "equal"}]},
            app_id="1661308505",
        )
        assert [e["key"] for e in job.filters["searchDsl"]] == ["other", "appid"]

    def test_rejects_a_duplicate_appid_clause(self):
        from app.schemas.jobs import JobCreate

        with pytest.raises(ValueError):
            JobCreate(
                filters={"searchDsl": [{"key": "appid", "value": "1", "type": "equal"}]},
                app_id="2",
            )

    def test_rejects_a_numeric_campaign(self):
        """`campaign` given a numeric store id returns ZERO rows with no
        error — verified. A silent empty result reads like "this app has no
        ads", so the request is refused instead."""
        from app.schemas.jobs import JobCreate

        with pytest.raises(ValueError) as info:
            JobCreate(filters={"purpose": 2, "campaign": "1661308505"})
        assert "app_id" in str(info.value)

    def test_allows_a_real_opaque_campaign_id(self):
        from app.schemas.jobs import JobCreate

        job = JobCreate(filters={"purpose": 2, "campaign": "M3OgFwcr4yLVdauQFomkHA=="})
        assert job.filters["campaign"] == "M3OgFwcr4yLVdauQFomkHA=="

    def test_no_app_id_leaves_filters_untouched(self):
        from app.schemas.jobs import JobCreate

        assert "searchDsl" not in JobCreate(filters={"purpose": 2}).filters

    def test_passes_arbitrary_filters_through(self):
        from app.schemas.jobs import JobCreate

        filters = {"purpose": 2, "media": [2], "area": ["TR"], "keyword": "x", "someNewFilter": 1}
        assert JobCreate(filters=filters).filters == filters


class TestCrypto:
    def test_round_trips_unicode(self):
        secret = "şifre-çğıöü-🔑"
        assert crypto.decrypt(crypto.encrypt(secret)) == secret

    def test_ciphertext_differs_per_call(self):
        assert crypto.encrypt("same") != crypto.encrypt("same")

    def test_optional_passthrough(self):
        assert crypto.encrypt_optional(None) is None
        assert crypto.decrypt_optional(None) is None

    def test_rejects_empty_ciphertext(self):
        with pytest.raises(ValueError):
            crypto.decrypt(b"")


class TestS3Keys:
    def test_namespaces_under_the_service_prefix(self):
        from app.core import s3 as s3lib

        key = s3lib.make_material_key("abc123", "video.mp4")
        assert key == "ad-scraper/materials/abc123/video.mp4"

    def test_sanitises_path_traversal_in_the_id(self):
        from app.core import s3 as s3lib

        key = s3lib.make_material_key("../../etc", "passwd")
        assert ".." not in key
        assert key.startswith("ad-scraper/materials/")

    def test_sanitises_separators_in_the_filename(self):
        from app.core import s3 as s3lib

        key = s3lib.make_material_key("abc", "a/b\\c.mp4")
        assert key == "ad-scraper/materials/abc/a_b_c.mp4"


class TestConfig:
    def test_row_ceiling_matches_the_api_limits(self):
        assert settings.max_rows_per_filter_set == settings.AD_MAX_PAGE * settings.AD_PAGE_SIZE
        assert settings.max_rows_per_filter_set == 10_000

    def test_mirror_defaults_to_always(self):
        """Signed source URLs expire in ~15 days, so mirroring is the point."""
        assert settings.AD_MIRROR_MEDIA == "always"

    def test_accept_language_has_a_value(self):
        # An empty value means HTTP 406 on every request.
        assert settings.AD_API_LANGUAGE
