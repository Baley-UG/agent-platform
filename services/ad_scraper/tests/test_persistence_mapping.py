"""Tests for the payload → column mapping, against a real captured page.

Pure functions only — no database. These are the tests that catch a
mis-mapped field, which is the failure mode most likely to go unnoticed
(the job succeeds, the column is just wrong).
"""

import pytest

from app.services.persistence.advertisers import extract_advertisers
from app.services.persistence.dimensions import extract_dimensions
from app.services.persistence.materials import build_material_params, extract_resources


class TestMaterialMapping:
    def test_maps_identity_and_type(self, video_material):
        params = build_material_params(video_material, job_id=None)
        assert params["id"] == video_material["id"]
        assert params["type"] == 202

    def test_duration_is_days_on_air_not_seconds(self, image_material):
        """The API's `duration` counts DAYS, and this is the field most
        likely to be misread as a video length.
        """
        params = build_material_params(image_material, job_id=None)
        assert params["run_days"] == 930
        assert params["start_date"].isoformat() == "2024-02-01"
        assert params["end_date"].isoformat() == "2026-08-18"
        # 2024-02-01 + 930 days lands on the end date, which is what proves
        # the semantic.
        assert (params["end_date"] - params["start_date"]).days == 929

    def test_video_length_comes_from_the_resource(self, video_material):
        params = build_material_params(video_material, job_id=None)
        assert params["media_duration_sec"] == 409
        # ...and is NOT the same number as run_days.
        assert params["run_days"] == 335

    def test_impressions_are_stored_raw_and_parsed(self, video_material):
        params = build_material_params(video_material, job_id=None)
        assert params["impression_inc_2y_raw"] == "1.8M"
        assert params["impression_inc_2y"] == 1_800_000

    def test_denormalises_the_primary_resource(self, video_material):
        params = build_material_params(video_material, job_id=None)
        assert params["media_format"] == "mp4"
        assert params["media_width"] == 360
        assert params["media_height"] == 640
        assert params["media_url"].startswith("https://")
        assert params["poster_url"].startswith("https://")

    def test_extracts_media_url_expiry(self, video_material):
        params = build_material_params(video_material, job_id=None)
        assert params["media_url_expires_at"] is not None
        assert params["media_url_expires_at"].tzinfo is not None

    def test_copy_comes_from_the_creative(self, image_material):
        params = build_material_params(image_material, job_id=None)
        assert params["slogan"] == "HD Video Downloader"

    def test_raw_is_serialised_json(self, video_material):
        params = build_material_params(video_material, job_id=None)
        assert isinstance(params["raw"], str)
        assert video_material["id"] in params["raw"]

    def test_empty_strings_become_null(self, image_material):
        # `txtUrl` is "" in the capture; storing that instead of NULL makes
        # every "has a landing page" query wrong.
        params = build_material_params(image_material, job_id=None)
        assert params["txt_url"] is None

    def test_tolerates_a_material_with_no_creative(self):
        params = build_material_params({"id": "abc", "type": 202}, job_id=None)
        assert params["id"] == "abc"
        assert params["media_url"] is None
        assert params["creative_type"] is None

    def test_violation_is_a_plain_label(self):
        """Observed as a bare string, e.g. "Human Exploitation" — so the
        column is text and `WHERE violation IS NOT NULL` is the whole query.
        """
        params = build_material_params({"id": "x", "violation": "Human Exploitation"}, job_id=None)
        assert params["violation"] == "Human Exploitation"

    def test_violation_absent_is_null(self):
        assert build_material_params({"id": "x"}, job_id=None)["violation"] is None
        assert build_material_params({"id": "x", "violation": ""}, job_id=None)["violation"] is None

    def test_violation_survives_a_structured_value(self):
        # A shape change must not drop the signal; JSON text keeps it readable.
        params = build_material_params({"id": "x", "violation": {"code": 7}}, job_id=None)
        assert params["violation"] == '{"code": 7}'


class TestResourceExtraction:
    def test_extracts_the_resource_array(self, video_material):
        resources = extract_resources(video_material)
        assert len(resources) == 1
        assert resources[0]["idx"] == 0
        assert resources[0]["format"] == "mp4"
        assert resources[0]["duration_sec"] == 409

    def test_returns_empty_for_a_material_without_resources(self):
        assert extract_resources({"id": "x"}) == []
        assert extract_resources({"id": "x", "creative": {}}) == []
        assert extract_resources({"id": "x", "creative": {"resource": None}}) == []

    def test_skips_non_dict_entries(self):
        material = {"id": "x", "creative": {"resource": ["garbage", {"format": "mp4"}]}}
        resources = extract_resources(material)
        assert len(resources) == 1
        assert resources[0]["format"] == "mp4"


class TestDimensionExtraction:
    def test_flattens_every_facet_array(self, video_material):
        dims = extract_dimensions(video_material)
        kinds = {d["kind"] for d in dims}
        assert {"media", "channel", "area", "format", "platform"} <= kinds

    def test_area_is_keyed_by_country_code(self, video_material):
        areas = [d for d in dims_of(video_material) if d["kind"] == "area"]
        assert {a["code"] for a in areas} == {"JP", "KR", "HK"}
        assert all(isinstance(a["code"], str) for a in areas)

    def test_numeric_facets_are_stringified(self, video_material):
        media = [d for d in dims_of(video_material) if d["kind"] == "media"]
        assert all(isinstance(m["code"], str) for m in media)
        assert "2" in {m["code"] for m in media}

    def test_resource_element_kind_is_snake_cased(self, video_material):
        """The wire key is `resourceElement`; storage uses `resource_element`."""
        kinds = {d["kind"] for d in dims_of(video_material)}
        assert "resource_element" in kinds
        assert "resourceElement" not in kinds

    def test_dedupes_within_a_facet(self):
        material = {"media": [{"id": 2, "name": "Facebook"}, {"id": 2, "name": "Facebook"}]}
        assert len(extract_dimensions(material)) == 1

    def test_drops_entries_without_an_identity(self):
        material = {"media": [{"name": "no id"}, {"id": None}, {"id": ""}, {"id": 2}]}
        dims = extract_dimensions(material)
        assert [d["code"] for d in dims] == ["2"]

    def test_tolerates_missing_and_malformed_facets(self):
        assert extract_dimensions({}) == []
        assert extract_dimensions({"media": None}) == []
        assert extract_dimensions({"media": "garbage"}) == []
        assert extract_dimensions({"media": ["garbage"]}) == []


class TestAdvertiserExtraction:
    def test_kind_comes_from_typename(self, video_material):
        advertisers = extract_advertisers(video_material)
        kinds = {a["kind"] for a in advertisers}
        assert kinds == {"AppBrand", "Website", "Playlet"}

    def test_kind_is_null_when_typename_is_absent(self):
        """We store NULL rather than inferring from shape — a NULL is
        visibly missing, a wrong guess is not.
        """
        material = {"campaign": [{"id": "x", "type": 401, "name": "example.com"}]}
        assert extract_advertisers(material)[0]["kind"] is None

    def test_extracts_appbrand_specific_fields(self, video_material):
        brand = next(a for a in extract_advertisers(video_material) if a["kind"] == "AppBrand")
        assert brand["name"]
        assert brand["types"] is None or all(isinstance(t, int) for t in brand["types"])
        assert brand["developer_id"] is not None

    def test_alias_is_a_list_not_a_string(self, video_material):
        """`alias` carries every localised store name — up to ten of them.

        Typed as a scalar it overflowed varchar(512) on the first real
        AppBrand row, which is why the column is `text[]`.
        """
        brand = next(a for a in extract_advertisers(video_material) if a["kind"] == "AppBrand")
        assert isinstance(brand["alias"], list)
        assert len(brand["alias"]) > 1
        assert all(isinstance(name, str) for name in brand["alias"])

    def test_alias_tolerates_a_bare_string(self):
        material = {"campaign": [{"id": "x", "__typename": "AppBrand", "alias": "Only One Name"}]}
        assert extract_advertisers(material)[0]["alias"] == ["Only One Name"]

    def test_empty_alias_becomes_null(self):
        # Websites and playlets send `alias: []`; storing an empty array
        # would make "has localised names" queries wrong.
        for value in ([], ["", "   "], None, "  "):
            material = {"campaign": [{"id": "x", "alias": value}]}
            assert extract_advertisers(material)[0]["alias"] is None, value

    def test_flattens_the_developer_object(self, video_material):
        brand = next(a for a in extract_advertisers(video_material) if a["kind"] == "AppBrand")
        assert brand["developer_name"]
        # `area.cc` is null in the capture for this developer — that is a
        # real shape, so the mapping must not require it.
        assert "developer_area_cc" in brand

    def test_drops_entries_without_an_id(self):
        material = {"campaign": [{"name": "no id"}, {"id": "keep", "name": "ok"}]}
        assert [a["id"] for a in extract_advertisers(material)] == ["keep"]

    def test_dedupes_repeated_ids(self):
        material = {"campaign": [{"id": "dup"}, {"id": "dup"}]}
        assert len(extract_advertisers(material)) == 1

    def test_tolerates_missing_and_malformed_campaign(self):
        assert extract_advertisers({}) == []
        assert extract_advertisers({"campaign": None}) == []
        assert extract_advertisers({"campaign": "garbage"}) == []
        assert extract_advertisers({"campaign": ["garbage"]}) == []

    def test_preserves_raw_entry(self, video_material):
        advertiser = extract_advertisers(video_material)[0]
        assert isinstance(advertiser["raw"], dict)
        assert advertiser["raw"]["id"] == advertiser["id"]


def dims_of(material):
    """Small helper so the facet tests read as one line each."""
    return extract_dimensions(material)
