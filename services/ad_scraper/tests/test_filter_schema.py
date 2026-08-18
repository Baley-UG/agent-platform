"""The filter schema — the panel's single source of truth for its form.

Before this endpoint the vocabulary lived in two places: a hand-written
option list in the panel and the measured truth here. They drifted. The
panel was missing nine networks it had never heard of and asserted four it
had guessed at (three of those guesses later measured correct, one — the
codes' *validity* — was not the panel's to decide anyway).

What matters most here is `MEDIA_VALID_CODES`. One invalid code fails the
WHOLE upstream request with `00:401001`, so a panel that offers a code
outside this set produces a job that cannot run.
"""

import pytest

from app.services.filter_schema import (
    FORMAT_SEED,
    MEDIA_SEED,
    MEDIA_VALID_CODES,
    PLATFORM_SEED,
    PURPOSE_OPTIONS,
    _merge,
)


class TestMediaValidCodes:
    def test_threads_is_valid(self):
        """The regression this test exists for: an earlier note recorded the
        range top as 31, which excluded 32. Threads answers 60M rows, so the
        endpoint would have told the panel to hide a working network."""
        assert 32 in MEDIA_VALID_CODES

    @pytest.mark.parametrize("code", [20, 24, 27, 35, 36])
    def test_measured_rejections_are_excluded(self, code):
        """Each of these came back `00:401001 Parameter error` from the live
        endpoint. Offering one fails the whole job."""
        assert code not in MEDIA_VALID_CODES

    @pytest.mark.parametrize("code", [1, 3, 7, 13, 19, 21, 25, 26, 29, 34])
    def test_measured_acceptances_are_included(self, code):
        assert code in MEDIA_VALID_CODES

    def test_every_named_network_is_valid(self):
        """A seed the upstream would reject is worse than no seed."""
        invalid = sorted(int(c) for c in MEDIA_SEED if int(c) not in MEDIA_VALID_CODES)
        assert invalid == [], f"seeded but not accepted upstream: {invalid}"


class TestSeeds:
    def test_networks_are_named_not_numbered(self):
        assert MEDIA_SEED["13"] == "TikTok"
        assert MEDIA_SEED["3"] == "X"
        assert MEDIA_SEED["21"] == "AdSense"

    def test_platform_is_the_operating_system(self):
        """The upstream's `platform` facet is the OS — the network is `media`.
        Getting these backwards is the single most common mix-up here."""
        assert set(PLATFORM_SEED.values()) == {"Android", "iOS"}

    def test_formats_are_placements(self):
        assert "Rewarded" in FORMAT_SEED.values()

    def test_purpose_has_exactly_three(self):
        assert [o["code"] for o in PURPOSE_OPTIONS] == [1, 2, 3]

    def test_purpose_names_match_the_upstream_enum(self):
        """Read out of the web app's own `purposeEnum`
        ({"Game":1,"App":2,"Website":3,...}) — not inferred. An earlier
        version of this file guessed "App advertisers / Broader mix / Web,
        social and display" from corpus sizes, which described the effect
        rather than the thing."""
        assert [o["name"] for o in PURPOSE_OPTIONS] == ["Game", "App", "Website"]

    def test_unavailable_purposes_are_recorded_not_offered(self):
        """4 and 5 are refused for this account, 6 answers zero rows. Kept out
        of the options, kept in the record so nobody re-probes them."""
        from app.services.filter_schema import PURPOSE_UNAVAILABLE

        assert set(PURPOSE_UNAVAILABLE) == {4, 5, 6}
        assert not set(PURPOSE_UNAVAILABLE) & {o["code"] for o in PURPOSE_OPTIONS}


class TestMerge:
    def test_observed_wins_over_seed(self):
        observed = [{"code": "13", "name": "TikTok Live", "icon": None, "material_count": 7}]
        merged = _merge(observed, {"13": "TikTok"})
        assert len(merged) == 1
        assert merged[0]["name"] == "TikTok Live"
        assert merged[0]["source"] == "observed"

    def test_seed_fills_a_gap(self):
        merged = _merge([], {"13": "TikTok"})
        assert merged[0] == {"code": "13", "name": "TikTok", "icon": None, "materials": 0, "source": "seed"}

    def test_observed_without_a_name_borrows_the_seed(self):
        """A dimension row can arrive with a null name; the seed still labels it."""
        merged = _merge([{"code": "13", "name": None, "icon": None, "material_count": 2}], {"13": "TikTok"})
        assert merged[0]["name"] == "TikTok"

    def test_most_used_first_and_unnamed_last(self):
        observed = [
            {"code": "1", "name": "Instagram", "icon": None, "material_count": 5},
            {"code": "2", "name": None, "icon": None, "material_count": 900},
            {"code": "3", "name": "X", "icon": None, "material_count": 50},
        ]
        codes = [o["code"] for o in _merge(observed, {})]
        assert codes == ["3", "1", "2"], "named by usage, then the unnamed one"


class TestDefaultPurpose:
    """`purpose` is pinned to a deployment default so callers need not carry it.

    The upstream requires it (`$purpose: Int!`) and offers no "all", so every
    job needs a value. Operators settled on one; the service supplies it.
    Pinning a default must not remove the capability, so an explicit value
    still wins.
    """

    def test_filled_in_when_absent(self):
        from app.core.config import settings
        from app.schemas.jobs import JobCreate

        assert JobCreate(filters={"media": [13]}).filters["purpose"] == settings.AD_DEFAULT_PURPOSE

    def test_filled_in_when_filters_is_omitted_entirely(self):
        """Pydantic skips validators on defaults unless told otherwise — this is
        the case that silently reached the upstream with no purpose."""
        from app.core.config import settings
        from app.schemas.jobs import JobCreate

        assert JobCreate().filters == {"purpose": settings.AD_DEFAULT_PURPOSE}

    def test_null_is_treated_as_absent(self):
        from app.core.config import settings
        from app.schemas.jobs import JobCreate

        assert JobCreate(filters={"purpose": None}).filters["purpose"] == settings.AD_DEFAULT_PURPOSE

    @pytest.mark.parametrize("explicit", [1, 2, 3])
    def test_an_explicit_value_always_wins(self, explicit):
        from app.schemas.jobs import JobCreate

        assert JobCreate(filters={"purpose": explicit}).filters["purpose"] == explicit

    def test_the_default_is_a_valid_purpose(self):
        from app.core.config import settings
        from app.services.filter_schema import PURPOSE_OPTIONS

        assert settings.AD_DEFAULT_PURPOSE in [o["code"] for o in PURPOSE_OPTIONS]


class TestUpstreamVocabularies:
    """Values taken from the web app's own source, not guessed.

    The whole reason these live here is that guessing them produced two wrong
    facts already: `purpose` described as a gradient, and `type` documented as
    only "102 image / 202 video".
    """

    def test_material_types_cover_more_than_image_and_video(self):
        from app.services.filter_schema import MATERIAL_TYPES

        # The pair our docs used to claim was the whole story...
        assert MATERIAL_TYPES["102"] == "Image"
        assert MATERIAL_TYPES["202"] == "Vertical Video"
        # ...and the ones they silently mislabelled.
        for code in ("100", "101", "103", "104", "105", "106", "201", "203", "301"):
            assert code in MATERIAL_TYPES

    def test_media_presets_match_the_ui_buttons(self):
        from app.services.filter_schema import MEDIA_PRESETS

        by_name = {p["name"]: p["media"] for p in MEDIA_PRESETS}
        assert by_name["Meta Ads"] == [2, 1, 10, 16, 32]
        assert by_name["Google Ads"] == [4, 11, 21]
        assert by_name["TikTok for Business"] == [13, 23, 18]

    def test_every_preset_code_is_valid(self):
        from app.services.filter_schema import MEDIA_PRESETS, MEDIA_VALID_CODES

        for preset in MEDIA_PRESETS:
            bad = [c for c in preset["media"] if c not in MEDIA_VALID_CODES]
            assert bad == [], f"{preset['name']} offers unusable codes: {bad}"

    def test_every_categorised_network_is_a_valid_code(self):
        from app.services.filter_schema import MEDIA_CATEGORY, MEDIA_VALID_CODES

        bad = sorted(int(c) for c in MEDIA_CATEGORY if int(c) not in MEDIA_VALID_CODES)
        assert bad == []

    def test_url_only_params_are_not_offered_as_filters(self):
        """`daterange` is the trap: it looks like a filter, reaches the
        GraphQL document as an undeclared variable, and is ignored."""
        from app.services.filter_schema import URL_ONLY_PARAMS

        assert "daterange" in URL_ONLY_PARAMS
        assert "advanced" in URL_ONLY_PARAMS

    def test_asr_languages_are_a_subset_of_ad_languages(self):
        from app.services.filter_schema import ASR_LANGUAGES, LANGUAGE_OPTIONS

        missing = [c for c in ASR_LANGUAGES if c not in LANGUAGE_OPTIONS]
        assert missing == [], f"voiceover languages with no label: {missing}"

    def test_known_orders_carry_the_ascending_variants(self):
        from app.schemas.jobs import KNOWN_ORDERS

        assert "max_dt" in KNOWN_ORDERS and "cnt_dt" in KNOWN_ORDERS
        assert "cnt_ad_id_desc" in KNOWN_ORDERS
