"""The list response's facet contract.

`GET /materials` used to return no facets at all, so nothing in a row said
whether the creative ran on TikTok or Facebook — `media_format` is the file
container (`mp4`), which reads like it should be the network and is not. A
table built on that response cannot show the one column everybody wants.

The queries themselves are Postgres-specific (`= ANY(:ids)`, window
functions) and this service has no Postgres test fixture, so what is pinned
here is the shape decision that governs payload size, plus the empty-page
short circuit. The query behaviour was verified against the live service:
a 3-row page came back with media TikTok, platform Android+iOS, channel
TikTok Ads, format In-Feed and the advertiser name, and a creative with 60
advertisers returned 5 with `advertiser_count: 60`.
"""

import pytest

from app.services.queries import _LIST_ADVERTISER_CAP, _LIST_FACET_KINDS, _attach_list_facets


class _ExplodingSession:
    """Any query at all is a failure for the empty-rows case."""

    def execute(self, *args, **kwargs):
        raise AssertionError("must not query the database for an empty page")


class TestListFacetContract:
    def test_area_is_not_a_list_facet(self):
        """Measured over 1 923 materials: `area` averages 56.1 edges and peaks
        at 136. On a 50-row page that is ~2 800 entries for a column no table
        renders. It stays on the detail endpoint."""
        assert "area" not in _LIST_FACET_KINDS

    def test_the_network_facet_is_present(self):
        """This is the one the question was about."""
        assert "media" in _LIST_FACET_KINDS

    def test_kept_facets_are_the_small_ones(self):
        # media 3.0, platform 1.4, channel 2.7, format 2.0 edges per material
        # on average — 9.1 combined, versus area's 56.1 alone.
        assert set(_LIST_FACET_KINDS) == {"media", "platform", "channel", "format"}

    def test_advertisers_are_capped(self):
        """One creative carried 60 advertisers. A table shows a few and a
        count; the cap must be small but non-zero."""
        assert 0 < _LIST_ADVERTISER_CAP <= 10

    def test_empty_page_does_no_work(self):
        assert _attach_list_facets(_ExplodingSession(), []) == []

    def test_resource_element_is_excluded(self):
        """3.6 edges per material of pixel-level creative annotations — real
        data, but not a table column."""
        assert "resource_element" not in _LIST_FACET_KINDS
