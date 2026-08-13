"""OIDC SSO unit tests — the pieces that don't need a live Authentik.

Covers the acceptance criteria that are testable offline:
  - duplicate users are never created (find_or_create is idempotent)
  - admin role derives from the `groups` claim (and demotes on re-login)
  - logout URL ends the Authentik session (end_session + id_token_hint)
  - PKCE pair is RFC 7636 S256-valid
  - unprotected access without a token/cookie is rejected (401 → the
    panel middleware converts that to a login redirect)
  - SSO users get an unusable local password

The full browser round-trip (redirect → Authentik → callback) is
covered by manual QA against the real IdP; these tests pin the logic.
"""

from __future__ import annotations

import base64
import hashlib
import os

import pytest

# Config must exist before app imports; provide inert OIDC env for tests.
os.environ.setdefault("OIDC_ISSUER", "https://internal.baley.eu/application/o/baley-admin")
os.environ.setdefault("OIDC_CLIENT_ID", "test-client")
os.environ.setdefault("OIDC_CLIENT_SECRET", "test-secret")
os.environ.setdefault("OIDC_REDIRECT_URI", "http://localhost:8000/auth/callback")
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-for-oidc-tests")

from sqlmodel import Session, SQLModel, create_engine, select  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.models.user import User  # noqa: E402
from app.services import oidc_service as oidc  # noqa: E402


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite://")
    # Only the tables this test touches — avoids pulling every model's
    # FK graph into sqlite.
    User.__table__.create(engine)
    try:
        from app.models.session import Session as ChatSession  # noqa: F401

        ChatSession.__table__.create(engine)
    except Exception:  # noqa: BLE001 — relationship table optional here
        pass
    with Session(engine) as s:
        yield s


CLAIMS = {
    "email": "Jane.Doe@Baley.EU",
    "name": "Jane Doe",
    "groups": ["staff"],
    "sub": "authentik-uid-1",
}


# ---------------------------------------------------------------------------
# find_or_create_user
# ---------------------------------------------------------------------------


def test_first_login_creates_user_with_normalized_email(db_session):
    user = oidc.find_or_create_user(db_session, CLAIMS)
    assert user.email == "jane.doe@baley.eu"
    assert user.name == "Jane Doe"
    assert user.role == "member"
    assert user.status == "active"
    assert user.last_login_at is not None


def test_second_login_does_not_duplicate(db_session):
    first = oidc.find_or_create_user(db_session, CLAIMS)
    second = oidc.find_or_create_user(db_session, CLAIMS)
    assert first.id == second.id
    rows = db_session.exec(select(User)).all()
    assert len(rows) == 1


def test_sso_user_password_is_unusable(db_session):
    user = oidc.find_or_create_user(db_session, CLAIMS)
    # Nothing plausible should verify — the hash is of a discarded secret.
    assert not user.verify_password("")
    assert not user.verify_password("password")
    assert not user.verify_password(user.email)


def test_missing_email_claim_rejected(db_session):
    with pytest.raises(oidc.OIDCError, match="email"):
        oidc.find_or_create_user(db_session, {"sub": "x", "groups": []})


# ---------------------------------------------------------------------------
# groups → role
# ---------------------------------------------------------------------------


def test_admin_group_grants_admin_role(db_session):
    claims = {**CLAIMS, "groups": ["staff", settings.OIDC_ADMIN_GROUP]}
    user = oidc.find_or_create_user(db_session, claims)
    assert user.role == "admin"


def test_role_resyncs_on_next_login(db_session):
    admin_claims = {**CLAIMS, "groups": [settings.OIDC_ADMIN_GROUP]}
    user = oidc.find_or_create_user(db_session, admin_claims)
    assert user.role == "admin"
    # Group revoked in Authentik → next login demotes.
    demoted = oidc.find_or_create_user(db_session, {**CLAIMS, "groups": ["staff"]})
    assert demoted.id == user.id
    assert demoted.role == "member"


def test_role_from_groups_pure():
    assert oidc.role_from_groups([settings.OIDC_ADMIN_GROUP]) == "admin"
    assert oidc.role_from_groups(["other"]) == "member"
    assert oidc.role_from_groups(None) == "member"
    assert oidc.role_from_groups("not-a-list") == "member"


# ---------------------------------------------------------------------------
# PKCE
# ---------------------------------------------------------------------------


def test_pkce_pair_is_s256_valid():
    verifier, challenge = oidc.generate_pkce_pair()
    assert 43 <= len(verifier) <= 128  # RFC 7636 §4.1
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    assert challenge == expected


def test_pkce_pairs_are_unique():
    pairs = {oidc.generate_pkce_pair()[0] for _ in range(20)}
    assert len(pairs) == 20


# ---------------------------------------------------------------------------
# Logout ends the Authentik session
# ---------------------------------------------------------------------------


def test_end_session_url_includes_hint_and_redirect(monkeypatch):
    monkeypatch.setattr(
        oidc,
        "get_discovery",
        lambda: {
            "authorization_endpoint": "https://idp/authorize",
            "token_endpoint": "https://idp/token",
            "jwks_uri": "https://idp/jwks",
            "end_session_endpoint": "https://idp/end-session",
        },
    )
    url = oidc.build_end_session_url("ID_TOKEN_JWT")
    assert url.startswith("https://idp/end-session?")
    assert "id_token_hint=ID_TOKEN_JWT" in url
    assert "post_logout_redirect_uri=" in url


def test_end_session_url_survives_discovery_outage(monkeypatch):
    def _boom():
        raise oidc.OIDCError("down")

    monkeypatch.setattr(oidc, "get_discovery", _boom)
    url = oidc.build_end_session_url(None)
    # Falls back to the local login page — logout never traps the user.
    assert url == settings.OIDC_POST_LOGOUT_REDIRECT_URI


# ---------------------------------------------------------------------------
# Unauthenticated access is rejected (the panel turns 401 into a
# login redirect)
# ---------------------------------------------------------------------------


def _import_admin_deps():
    """Import admin_deps without touching a real Postgres.

    `app.services.database` connects eagerly at import (singleton pool +
    create_all). Stub it in sys.modules BEFORE the admin_deps import so
    unit tests stay infra-free. The deps under test never use the
    engine — they take an explicit session parameter.
    """
    import sys
    import types

    if "app.services.database" not in sys.modules:
        fake = types.ModuleType("app.services.database")

        class _FakeDBService:  # pragma: no cover - inert stand-in
            engine = None

        fake.database_service = _FakeDBService()
        sys.modules["app.services.database"] = fake
    from app.api.v1 import admin_deps

    return admin_deps


def test_admin_dep_rejects_missing_credentials():
    from fastapi import HTTPException

    admin_deps = _import_admin_deps()

    with pytest.raises(HTTPException) as exc_info:
        admin_deps.require_admin_token(
            authorization=None, baley_admin_session=None, session=None
        )
    assert exc_info.value.status_code == 401


def test_admin_dep_accepts_cookie(db_session):
    """The session cookie carries the same JWT the Bearer path uses."""
    admin_deps = _import_admin_deps()
    from app.utils import admin_auth as core

    user = oidc.find_or_create_user(db_session, CLAIMS)
    token, _ = core.issue_access_token(user.id, user.role, ttl_minutes=480)
    principal = admin_deps.require_admin_token(
        authorization=None, baley_admin_session=token, session=db_session
    )
    assert principal.user.id == user.id
