"""Tests for the in-process session-token cache.

The cache is what makes the token approach cheap: without it every request
costs a DB round-trip plus a Fernet decrypt. Its correctness rests entirely
on invalidating at the right moments, which is what these pin.

No database — the tests drive `_cache_store` / `_cache_read` directly and
build `Credential` rows in memory.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.core.config import settings
from app.models.credential import ACTIVE, DISABLED, EXPIRED, LOGIN_FAILED, Credential
from app.services import credentials as creds


@pytest.fixture(autouse=True)
def _clean_cache():
    """Every test starts and ends with an empty cache."""
    creds.invalidate_cache("test_setup")
    yield
    creds.invalidate_cache("test_teardown")


def _future(seconds: int) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


class TestCacheReadWrite:
    def test_round_trips_a_token(self):
        creds._cache_store("default", "tok-123", _future(86_400))
        assert creds._cache_read("default") == "tok-123"

    def test_miss_on_empty_cache(self):
        assert creds._cache_read("default") is None

    def test_miss_on_a_different_label(self):
        """A second seat must not be served another seat's token."""
        creds._cache_store("default", "tok-a", _future(86_400))
        assert creds._cache_read("other") is None

    def test_invalidate_clears_it(self):
        creds._cache_store("default", "tok-123", _future(86_400))
        creds.invalidate_cache("test")
        assert creds._cache_read("default") is None


class TestStaleness:
    def test_unknown_expiry_is_not_stale(self):
        """A token whose `exp` we couldn't parse may be perfectly good.

        Treating it as stale would add a DB read per request and gain
        nothing — a rejection from the API invalidates it instead.
        """
        creds._cache_store("default", "tok", None)
        assert creds._cache_read("default") == "tok"

    def test_token_inside_the_refresh_margin_is_dropped(self):
        margin = settings.AD_SESSION_REFRESH_MARGIN_SECONDS
        creds._cache_store("default", "tok", _future(margin // 2))
        assert creds._cache_read("default") is None, "should not serve a token about to die mid-job"

    def test_token_outside_the_margin_is_served(self):
        margin = settings.AD_SESSION_REFRESH_MARGIN_SECONDS
        creds._cache_store("default", "tok", _future(margin * 3))
        assert creds._cache_read("default") == "tok"

    def test_already_expired_token_is_dropped(self):
        creds._cache_store("default", "tok", _future(-3600))
        assert creds._cache_read("default") is None

    def test_naive_expiry_is_treated_as_utc(self):
        """Postgres can hand back a naive datetime depending on the driver;
        comparing it to an aware `now` would raise."""
        naive = (datetime.now(timezone.utc) + timedelta(days=5)).replace(tzinfo=None)
        creds._cache_store("default", "tok", naive)
        assert creds._cache_read("default") == "tok"

    def test_a_stale_read_also_clears_the_entry(self):
        creds._cache_store("default", "tok", _future(-1))
        creds._cache_read("default")
        # Second read must not resurrect it even before staleness is re-checked.
        assert creds._cache is None


class TestNeedsRefresh:
    def test_no_token_needs_refresh(self):
        assert creds.needs_refresh(Credential(label="default", status=EXPIRED)) is True

    def test_unknown_expiry_does_not(self):
        row = Credential(label="default", status=ACTIVE, session_cookie_enc=b"x", session_expires_at=None)
        assert creds.needs_refresh(row) is False

    def test_inside_margin_does(self):
        margin = settings.AD_SESSION_REFRESH_MARGIN_SECONDS
        row = Credential(
            label="default", status=ACTIVE, session_cookie_enc=b"x", session_expires_at=_future(margin // 2)
        )
        assert creds.needs_refresh(row) is True

    def test_outside_margin_does_not(self):
        margin = settings.AD_SESSION_REFRESH_MARGIN_SECONDS
        row = Credential(
            label="default", status=ACTIVE, session_cookie_enc=b"x", session_expires_at=_future(margin * 3)
        )
        assert creds.needs_refresh(row) is False

    def test_naive_expiry_does_not_raise(self):
        naive = (datetime.now(timezone.utc) + timedelta(days=5)).replace(tzinfo=None)
        row = Credential(label="default", status=ACTIVE, session_cookie_enc=b"x", session_expires_at=naive)
        assert creds.needs_refresh(row) is False


class TestRedactedView:
    def test_never_exposes_the_token(self):
        row = Credential(
            label="default", status=ACTIVE, session_cookie_enc=b"super-secret", session_expires_at=_future(86_400)
        )
        view = creds.redacted_view(row)
        assert "session_cookie_enc" not in view
        assert b"super-secret" not in repr(view).encode()
        assert view["has_session"] is True

    def test_reports_remaining_lifetime(self):
        row = Credential(label="default", status=ACTIVE, session_cookie_enc=b"x", session_expires_at=_future(3600))
        view = creds.redacted_view(row)
        assert 3500 < view["expires_in_seconds"] <= 3600

    def test_reports_negative_lifetime_for_a_dead_token(self):
        """Surfaced rather than clamped, so a panel can say how long ago."""
        row = Credential(label="default", status=EXPIRED, session_cookie_enc=b"x", session_expires_at=_future(-7200))
        assert creds.redacted_view(row)["expires_in_seconds"] < 0

    def test_none_row_yields_a_usable_empty_view(self):
        view = creds.redacted_view(None)
        assert view["has_session"] is False
        assert view["needs_refresh"] is True
        assert view["status"] == EXPIRED

    def test_no_password_fields_remain(self):
        """Automatic login was dropped; nothing should hint at a stored one."""
        view = creds.redacted_view(None)
        assert "has_password" not in view
        assert "username" not in view


class TestCredentialModel:
    def test_has_no_password_columns(self):
        """No stored password means none to leak and no lockout risk."""
        columns = set(Credential.model_fields)
        assert "password_enc" not in columns
        assert "username" not in columns

    def test_status_vocabulary(self):
        from app.models.credential import VALID_CREDENTIAL_STATUSES

        assert VALID_CREDENTIAL_STATUSES == {ACTIVE, EXPIRED, LOGIN_FAILED, DISABLED}
