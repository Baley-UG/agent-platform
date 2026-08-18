"""Tests for the job-runner's pure decision logic.

The truncation signal is the one worth pinning hardest: the API's 10 000-row
ceiling means a broad filter silently returns a fraction of its matches, and
a job that reported plain success would be actively misleading.
"""

import pytest

from app.core.config import settings
from app.services import mirror
from app.services.ingest import IngestStats, _note_truncation


class TestTruncationSignal:
    def test_flags_a_filter_beyond_the_hard_ceiling(self):
        stats = IngestStats()
        _note_truncation(stats, page_to=200, total=135_158_767)
        assert stats.truncated is True
        assert stats.total_reported == 135_158_767
        # The note must tell the operator what to do, not just that it happened.
        assert "Partition the filter" in stats.notes[0]

    def test_flags_a_window_narrower_than_the_result_set(self):
        stats = IngestStats()
        _note_truncation(stats, page_to=2, total=500)
        assert stats.truncated is True
        assert "Raise page_to" in stats.notes[0]

    def test_distinguishes_the_two_cases(self):
        """Under the ceiling → "raise page_to". Over it → "partition"."""
        reachable = IngestStats()
        _note_truncation(reachable, page_to=2, total=500)
        unreachable = IngestStats()
        _note_truncation(unreachable, page_to=200, total=50_000)
        assert "Raise page_to" in reachable.notes[0]
        assert "Raise page_to" not in unreachable.notes[0]

    def test_quiet_when_the_window_covers_everything(self):
        stats = IngestStats()
        _note_truncation(stats, page_to=5, total=120)
        assert stats.truncated is False
        assert stats.notes == []
        assert stats.total_reported == 120

    def test_exactly_reachable_is_not_truncated(self):
        stats = IngestStats()
        _note_truncation(stats, page_to=2, total=2 * settings.AD_PAGE_SIZE)
        assert stats.truncated is False

    def test_one_row_over_is_truncated(self):
        stats = IngestStats()
        _note_truncation(stats, page_to=2, total=2 * settings.AD_PAGE_SIZE + 1)
        assert stats.truncated is True

    def test_missing_total_is_not_an_error(self):
        stats = IngestStats()
        _note_truncation(stats, page_to=5, total=None)
        assert stats.truncated is False
        assert stats.total_reported is None

    def test_note_is_not_duplicated_across_pages(self):
        """Every page of a broad filter reports the same `total`."""
        stats = IngestStats()
        for _ in range(5):
            _note_truncation(stats, page_to=200, total=1_000_000)
        assert len(stats.notes) == 1


class TestStatsSerialisation:
    def test_as_dict_carries_every_counter(self):
        stats = IngestStats(pages_fetched=2, materials_seen=100, materials_new=90, materials_updated=10)
        payload = stats.as_dict()
        for key in (
            "pages_fetched",
            "materials_seen",
            "materials_new",
            "materials_updated",
            "materials_skipped",
            "mirrored",
            "mirror_failed",
            "mirror_skipped",
            "total_reported",
            "truncated",
            "notes",
        ):
            assert key in payload

    def test_as_dict_is_json_safe(self):
        import json

        json.dumps(IngestStats().as_dict())


class TestMirrorPolicy:
    def test_always_mirrors_by_default(self, monkeypatch):
        monkeypatch.setattr(settings, "AD_MIRROR_MEDIA", "always")
        assert mirror.should_mirror(job_mirror=None) is True

    def test_never_skips_regardless_of_the_job_flag(self, monkeypatch):
        monkeypatch.setattr(settings, "AD_MIRROR_MEDIA", "never")
        assert mirror.should_mirror(job_mirror=True) is False

    def test_job_mode_defers_to_the_job(self, monkeypatch):
        monkeypatch.setattr(settings, "AD_MIRROR_MEDIA", "job")
        assert mirror.should_mirror(job_mirror=True) is True
        assert mirror.should_mirror(job_mirror=False) is False

    def test_case_insensitive(self, monkeypatch):
        monkeypatch.setattr(settings, "AD_MIRROR_MEDIA", "NEVER")
        assert mirror.should_mirror(job_mirror=True) is False

    def test_unknown_policy_fails_open(self, monkeypatch):
        """An env typo must not silently stop mirroring — the source URLs
        expire, so a skipped mirror is unrecoverable. An explicit per-job
        opt-out is still honoured; only the policy default fails open.
        """
        monkeypatch.setattr(settings, "AD_MIRROR_MEDIA", "sometimes")
        assert mirror.should_mirror(job_mirror=None) is True
        assert mirror.should_mirror(job_mirror=False) is False


class TestMirrorOverride:
    """An explicit per-job flag must beat the policy default.

    Under `always` a job asking `mirror=False` used to be accepted and then
    quietly mirrored anyway — the same failure shape as a filter that returns
    nothing without erroring.
    """

    def test_always_honours_an_explicit_optout(self, monkeypatch):
        monkeypatch.setattr(settings, "AD_MIRROR_MEDIA", "always")
        assert mirror.should_mirror(job_mirror=False) is False

    def test_always_mirrors_when_unspecified(self, monkeypatch):
        monkeypatch.setattr(settings, "AD_MIRROR_MEDIA", "always")
        assert mirror.should_mirror(job_mirror=None) is True

    def test_job_policy_needs_an_explicit_optin(self, monkeypatch):
        monkeypatch.setattr(settings, "AD_MIRROR_MEDIA", "job")
        assert mirror.should_mirror(job_mirror=None) is False
        assert mirror.should_mirror(job_mirror=True) is True

    def test_never_ignores_an_explicit_optin(self, monkeypatch):
        """Turning storage off is an operator decision a job can't override."""
        monkeypatch.setattr(settings, "AD_MIRROR_MEDIA", "never")
        assert mirror.should_mirror(job_mirror=True) is False


class TestAlreadyMirrored:
    """Re-running a filter is the normal way to catch new creatives, so a
    re-run must not re-download everything it already holds."""

    def test_result_reports_an_existing_mirror(self):
        from app.services.persistence.materials import UpsertResult

        r = UpsertResult(material_id="m", outcome="updated", existing_media_key="ad-scraper/materials/m/v.mp4")
        assert r.already_mirrored is True

    def test_result_without_a_key_is_not_mirrored(self):
        from app.services.persistence.materials import UpsertResult

        assert UpsertResult(material_id="m", outcome="new").already_mirrored is False

    def test_stats_carry_the_cached_counter(self):
        assert "mirror_cached" in IngestStats().as_dict()


class TestImageDetection:
    @pytest.mark.parametrize("path", ["a.jpg", "a.jpeg", "a.PNG", "a.webp", "a.gif"])
    def test_recognises_image_extensions(self, path):
        assert mirror._looks_like_image(f"https://cdn/{path}?auth_key=1-x", f"key/{path}") is True

    def test_video_is_not_an_image(self):
        assert mirror._looks_like_image("https://cdn/a.mp4?auth_key=1-x", "key/a.mp4") is False

    def test_ignores_the_query_string(self):
        # A .jpg in the signature must not make an mp4 look like an image.
        assert mirror._looks_like_image("https://cdn/a.mp4?x=b.jpg", "key/a.mp4") is False
