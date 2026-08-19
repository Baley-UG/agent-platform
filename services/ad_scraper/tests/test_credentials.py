"""Credential helpers: expiry margin, redaction, and the model's shape.

Formerly `test_token_cache.py`. The cache it tested is gone — see the note in
`app/services/credentials.py`. A module-level cache is per PROCESS, and this
service runs two: the API would store a rotated token and prime its own copy
while the worker kept using the old one until a job failed. What it bought,
measured, was 0.66 ms per read against a rate gate that already spaces
requests 1500 ms apart.

`TestNoCaching` is the regression guard for that.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.core.config import settings
from app.models.credential import ACTIVE, DISABLED, EXPIRED, LOGIN_FAILED, Credential
from app.services import credentials as creds


def _future(seconds: int) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


class TestNoCaching:
    """A rotated token must take effect in every process on the next read."""

    def test_the_cache_helpers_are_gone(self):
        """Named explicitly: reintroducing any of these brings the
        cross-process staleness back with them."""
        for gone in ("invalidate_cache", "_cache_store", "_cache_read", "_CachedToken"):
            assert not hasattr(creds, gone), f"{gone} is back — see the note in credentials.py"

    def test_every_read_hits_the_row(self, monkeypatch):
        """Two consecutive reads must both load the row, so a token rotated
        between them is picked up rather than served stale from memory."""
        import contextlib

        loads = []

        @contextlib.contextmanager
        def fake_scope():
            yield object()

        class _Row:
            session_cookie_enc = b"enc"
            status = "active"
            session_expires_at = _future(86_400)

        def fake_get(session, label="default"):
            loads.append(label)
            return _Row()

        monkeypatch.setattr(creds, "session_scope", fake_scope)
        monkeypatch.setattr(creds, "get_credential", fake_get)
        monkeypatch.setattr(creds.crypto, "decrypt", lambda blob: "tok-from-row")

        assert creds.current_cookie() == "tok-from-row"
        assert creds.current_cookie() == "tok-from-row"
        assert len(loads) == 2, "second read came from memory — the cache is back"

    def test_a_rotated_token_is_seen_immediately(self, monkeypatch):
        import contextlib

        @contextlib.contextmanager
        def fake_scope():
            yield object()

        tokens = iter(["old-token", "new-token"])

        class _Row:
            session_cookie_enc = b"enc"
            status = "active"
            session_expires_at = _future(86_400)

        monkeypatch.setattr(creds, "session_scope", fake_scope)
        monkeypatch.setattr(creds, "get_credential", lambda session, label="default": _Row())
        monkeypatch.setattr(creds.crypto, "decrypt", lambda blob: next(tokens))

        assert creds.current_cookie() == "old-token"
        assert creds.current_cookie() == "new-token", "worker would keep the stale token"


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
