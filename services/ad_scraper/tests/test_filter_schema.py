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

    def test_purpose_one_is_labelled_as_the_app_corpus(self):
        """It was the unlabelled option in the panel, and it is the corpus
        this service exists to browse."""
        assert "App" in PURPOSE_OPTIONS[0]["name"]


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
