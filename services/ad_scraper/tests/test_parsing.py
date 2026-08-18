"""Tests for the payload parsers.

These pin the quirks that made a naive mapping wrong: display-formatted
impression counts, string-typed integers, the signed-URL expiry, and the
JWT expiry claim.
"""

from datetime import datetime, timezone

import pytest

from app.services.parsing import (
    expires_at_from_auth_key,
    filename_from_url,
    jwt_expires_at,
    parse_compact_number,
    parse_date,
    parse_int,
)


class TestParseCompactNumber:
    """`impression_inc_2y` arrives as "1.1M", which sorts alphabetically."""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("1.1M", 1_100_000),
            ("476.3K", 476_300),
            ("2.4B", 2_400_000_000),
            ("930", 930),
            ("1,234", 1234),
            ("1.5T", 1_500_000_000_000),
            ("  3.3M  ", 3_300_000),
            ("1m", 1_000_000),  # lowercase suffix
            (1234, 1234),
            (12.7, 12),
        ],
    )
    def test_parses(self, raw, expected):
        assert parse_compact_number(raw) == expected

    @pytest.mark.parametrize(
        "raw,expected",
        [
            (">10M", 10_000_000),
            ("> 10M", 10_000_000),
            ("<1K", 1_000),
            ("~5M", 5_000_000),
            ("10M+", 10_000_000),
        ],
    )
    def test_parses_bounded_forms(self, raw, expected):
        """The platform caps its display at ">10M" for the top performers.

        Leaving those unparsed would NULL `impression_inc_2y` for exactly the
        creatives most likely to matter, dropping them out of every
        min_impressions threshold and impressions sort.
        """
        assert parse_compact_number(raw) == expected

    @pytest.mark.parametrize("raw", [None, "", "   ", "N/A", "abc", "--", ">", "~", True, False])
    def test_returns_none_for_unusable(self, raw):
        assert parse_compact_number(raw) is None

    def test_rounds_rather_than_truncates(self):
        # 476.35K would truncate to 476_349 with int(); we want 476_350.
        assert parse_compact_number("476.35K") == 476_350


class TestParseInt:
    """`duration` and `similar_cnt` come over the wire as strings."""

    @pytest.mark.parametrize(
        "raw,expected",
        [("930", 930), ("0", 0), (7, 7), ("12.9", 12), ("", None), (None, None), ("abc", None), (True, None)],
    )
    def test_coerces(self, raw, expected):
        assert parse_int(raw) == expected


class TestParseDate:
    def test_parses_iso(self):
        assert parse_date("2024-02-01").isoformat() == "2024-02-01"

    def test_tolerates_datetime_string(self):
        assert parse_date("2024-02-01T10:00:00Z").isoformat() == "2024-02-01"

    @pytest.mark.parametrize("raw", [None, "", "not-a-date", "2024-13-45"])
    def test_returns_none_for_unusable(self, raw):
        assert parse_date(raw) is None


class TestExpiresAtFromAuthKey:
    """Signed CDN URLs carry their own expiry as the first auth_key field."""

    def test_extracts_epoch(self):
        got = expires_at_from_auth_key("https://cdn.example/x.mp4?auth_key=1788334146-abc-0-def")
        assert got == datetime(2026, 9, 2, 7, 29, 6, tzinfo=timezone.utc)

    def test_survives_extra_query_params(self):
        got = expires_at_from_auth_key("https://cdn.example/x.mp4?v=2&auth_key=1788334146-abc-0-def&t=1")
        assert got is not None and got.year == 2026

    @pytest.mark.parametrize(
        "url",
        [
            None,
            "",
            "https://cdn.example/x.mp4",
            "https://cdn.example/x.mp4?auth_key=",
            "https://cdn.example/x.mp4?auth_key=notanumber-abc",
        ],
    )
    def test_returns_none_without_a_usable_epoch(self, url):
        assert expires_at_from_auth_key(url) is None

    def test_result_is_timezone_aware(self):
        got = expires_at_from_auth_key("https://cdn.example/x.mp4?auth_key=1788334146-a-0-b")
        assert got.tzinfo is not None


class TestJwtExpiresAt:
    """We read `exp` without verifying the signature — see the docstring."""

    # Real structure, throwaway payload: {"jti":"x","exp":1787642915,"sub":"1"}
    _TOKEN = (
        "eyJ0eXAiOiJKV1QiLCJhbGciOiJFUzI1NiJ9"
        ".eyJqdGkiOiJ4IiwiZXhwIjoxNzg3NjQyOTE1LCJzdWIiOiIxIn0"
        ".c2lnbmF0dXJlLWlzLW5vdC1jaGVja2Vk"
    )

    def test_reads_exp_claim(self):
        got = jwt_expires_at(self._TOKEN)
        assert got == datetime(2026, 8, 25, 7, 28, 35, tzinfo=timezone.utc)

    def test_ignores_invalid_signature(self):
        # The point of the function: a token we cannot verify still tells us
        # when the server will stop accepting it.
        tampered = self._TOKEN.rsplit(".", 1)[0] + ".dGFtcGVyZWQ"
        assert jwt_expires_at(tampered) is not None

    @pytest.mark.parametrize("token", [None, "", "not-a-jwt", "a.b", "a.!!!notbase64!!!.c"])
    def test_returns_none_for_unusable(self, token):
        assert jwt_expires_at(token) is None

    def test_returns_none_when_exp_missing(self):
        # {"jti":"x"} — no exp claim.
        no_exp = "eyJhbGciOiJFUzI1NiJ9.eyJqdGkiOiJ4In0.sig"
        assert jwt_expires_at(no_exp) is None


class TestFilenameFromUrl:
    def test_drops_the_query_string(self):
        # Otherwise every re-mirror would land under a different S3 key.
        name = filename_from_url("https://cdn.example/mp4/ab/cd/abcd.mp4?auth_key=123-x-0-y")
        assert name == "abcd.mp4"

    def test_adds_extension_when_missing(self):
        assert filename_from_url("https://cdn.example/asset", ".mp4") == "asset.mp4"

    def test_falls_back_when_path_is_empty(self):
        assert filename_from_url("https://cdn.example/", ".jpg") == "asset.jpg"

    def test_handles_none(self):
        assert filename_from_url(None, ".bin") == "asset.bin"
