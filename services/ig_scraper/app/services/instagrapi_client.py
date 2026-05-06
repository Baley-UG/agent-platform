"""Thin wrapper around instagrapi's Client.

instagrapi is fully synchronous and uses HTTP under the hood. We wrap
the blocking calls in `asyncio.to_thread` so they can be awaited from
FastAPI endpoints without holding the event loop.

Only the *infrastructure* concerns live here: device fingerprint,
session blob loading, proxy URL construction, and the login flow.
Higher-level scraping logic (feed/story/hashtag fetchers) lands in
M4–M6 and lives in its own modules.

NOTE: instagrapi's exception classes vary by version. We catch with
broad type guards (`type(exc).__name__`) so this code keeps working
when the library bumps.
"""

import asyncio
from dataclasses import dataclass
from typing import Optional

from app.core.logging import logger
from app.models.account import Account
from app.models.proxy import Proxy
from app.services.crypto import decrypt, decrypt_optional


def _import_client():
    """Lazy import: instagrapi pulls in heavy deps (PIL, moviepy)."""
    from instagrapi import Client  # type: ignore

    return Client


def build_proxy_url(proxy: Optional[Proxy]) -> Optional[str]:
    """Render a Proxy row as a URL instagrapi understands.

    Returns None when no proxy is bound — the worker rejects those
    accounts in production, but for `proxy_test`-style flows we may
    skip this entirely.
    """
    if proxy is None:
        return None
    creds = ""
    if proxy.username:
        password = decrypt_optional(proxy.password_enc) or ""
        creds = f"{proxy.username}:{password}@"
    return f"{proxy.protocol}://{creds}{proxy.host}:{proxy.port}"


@dataclass
class LoginOutcome:
    """Structured result of a login attempt.

    `status` matches the values stored on `ig_accounts.status`:
      active | challenge_required | banned | disabled
    """

    status: str
    session_blob: Optional[dict]
    detail: Optional[str]


def _classify_login_exception(exc: Exception) -> tuple[str, str]:
    """Map an instagrapi exception to (account_status, human_detail)."""
    name = type(exc).__name__
    if name in {"ChallengeRequired", "RecaptchaChallengeForm", "SelectContactPointRecoveryForm"}:
        return "challenge_required", f"Challenge required: {exc}"
    if name in {"BadPassword", "TwoFactorRequired"}:
        # TwoFactorRequired is "operator must supply the code" — same
        # human handoff as a challenge.
        return "challenge_required", f"Manual verification needed: {name}"
    if name in {"UserNotFound"}:
        return "disabled", "Username not found on Instagram."
    if name in {"PleaseWaitFewMinutes", "RateLimitError"}:
        return "challenge_required", "Rate-limited at login — try again later or rotate proxy."
    if name in {"FeedbackRequired"}:
        return "banned", f"Action blocked / feedback required: {exc}"
    return "disabled", f"{name}: {exc}"


def _do_login_sync(
    username: str,
    password: str,
    session_blob: Optional[dict],
    proxy_url: Optional[str],
    verification_code: Optional[str],
) -> LoginOutcome:
    """Blocking login flow — must run inside asyncio.to_thread."""
    Client = _import_client()
    client = Client()

    # Sticky device fingerprint: load from session_blob if we have one.
    if session_blob:
        try:
            client.set_settings(session_blob)
        except Exception as exc:  # noqa: BLE001
            logger.warning("instagrapi_set_settings_failed", error=str(exc), username=username)

    if proxy_url:
        try:
            client.set_proxy(proxy_url)
        except Exception as exc:  # noqa: BLE001
            return LoginOutcome(status="disabled", session_blob=None, detail=f"Proxy rejected: {exc}")

    try:
        if verification_code:
            client.login(username, password, verification_code=verification_code)
        else:
            client.login(username, password)
    except Exception as exc:  # noqa: BLE001
        status, detail = _classify_login_exception(exc)
        # Even on a soft failure we may have collected a partial settings
        # blob (device fingerprint, etc.); persist it so retries land on
        # the same identity.
        partial = None
        try:
            partial = client.get_settings()
        except Exception:  # noqa: BLE001
            partial = None
        return LoginOutcome(status=status, session_blob=partial, detail=detail)

    # Cheap post-login probe — getting your own user_id confirms the
    # session actually works and surfaces "shadow login" failures
    # instagrapi sometimes lets through.
    try:
        client.get_timeline_feed()
    except Exception as exc:  # noqa: BLE001
        status, detail = _classify_login_exception(exc)
        return LoginOutcome(
            status=status,
            session_blob=client.get_settings(),
            detail=f"Login accepted but probe failed: {detail}",
        )

    return LoginOutcome(status="active", session_blob=client.get_settings(), detail="ok")


async def login_account(
    account: Account,
    proxy: Optional[Proxy],
    verification_code: Optional[str] = None,
) -> LoginOutcome:
    """Run the login flow off the event loop and return a structured outcome."""
    plaintext_password = decrypt(account.password_enc)
    proxy_url = build_proxy_url(proxy)
    return await asyncio.to_thread(
        _do_login_sync,
        account.username,
        plaintext_password,
        account.session_blob,
        proxy_url,
        verification_code,
    )
