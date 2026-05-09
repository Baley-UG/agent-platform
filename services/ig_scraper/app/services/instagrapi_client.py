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
from typing import Dict, Optional

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
      active | cooldown | challenge_required | banned | disabled
    `detail` is the operator-facing summary; `ig_message` is IG's
    verbatim response message; `error_type` is IG's machine-readable
    code (e.g. 'bad_password', 'checkpoint_required').
    """

    status: str
    session_blob: Optional[dict]
    detail: Optional[str]
    ig_message: Optional[str] = None
    error_type: Optional[str] = None
    exception_name: Optional[str] = None


def _extract_ig_response(exc: Exception) -> tuple[Optional[str], Optional[str]]:
    """Pull (ig_message, error_type) out of an instagrapi exception.

    instagrapi attaches the parsed IG response body as `exc.last_json`
    (sometimes also `exc.response.json()`). We try both. Returns
    (None, None) if neither is available.
    """
    last_json = getattr(exc, "last_json", None)
    if not isinstance(last_json, dict):
        response = getattr(exc, "response", None)
        if response is not None:
            try:
                last_json = response.json()
            except Exception:  # noqa: BLE001
                last_json = None
    if not isinstance(last_json, dict):
        return None, None
    return last_json.get("message"), last_json.get("error_type")


def _classify_login_exception(exc: Exception) -> tuple[str, str, Optional[str], Optional[str]]:
    """Map an instagrapi exception to (status, detail, ig_message, error_type).

    `detail` is the operator-facing diagnostic. It always includes IG's
    exact message when available, plus a hint about likely causes.
    """
    name = type(exc).__name__
    ig_message, error_type = _extract_ig_response(exc)
    quoted = f'"{ig_message}"' if ig_message else str(exc)

    if name in {"ChallengeRequired", "RecaptchaChallengeForm", "SelectContactPointRecoveryForm"}:
        detail = (
            f"Challenge required by Instagram. IG says: {quoted}. "
            f"If a code was sent (SMS / email), retry login with "
            f"verification_code. If no code arrived, IG is soft-blocking "
            f"this device/IP — try a residential proxy or warm the "
            f"account from a real device first."
        )
        return "challenge_required", detail, ig_message, error_type

    if name == "TwoFactorRequired":
        return (
            "challenge_required",
            f"Two-factor required. IG says: {quoted}. "
            f"Retry POST /login with `verification_code` set to the 6-digit code from your authenticator.",
            ig_message,
            error_type,
        )

    if name in {"BadPassword", "UserInvalidCredentials"}:
        detail = (
            f"Bad password. IG says: {quoted}. "
            f"Possibilities: (1) the stored password is genuinely wrong "
            f"→ PATCH /accounts/{{id}} with the correct one; "
            f"(2) password is right but IG is soft-blocking this IP — "
            f"attach a residential / mobile proxy and retry; "
            f"(3) IG flagged this device's fingerprint — try a fresh "
            f"account already logged in via the official app."
        )
        return "disabled", detail, ig_message, error_type

    if name == "UserNotFound":
        return (
            "disabled",
            f"Username not found on Instagram. IG says: {quoted}",
            ig_message,
            error_type,
        )

    if name in {"PleaseWaitFewMinutes", "RateLimitError"}:
        return (
            "cooldown",
            f"Rate-limited at login. IG says: {quoted}. "
            f"Wait a few minutes, ideally rotate proxy before retry.",
            ig_message,
            error_type,
        )

    if name == "FeedbackRequired":
        return (
            "banned",
            f"Action blocked / feedback required. IG says: {quoted}. "
            f"Account is likely flagged — investigate manually before further attempts.",
            ig_message,
            error_type,
        )

    return (
        "disabled",
        f"{name}: {quoted}",
        ig_message,
        error_type,
    )


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
        status, detail, ig_message, error_type = _classify_login_exception(exc)
        # Even on a soft failure we may have collected a partial settings
        # blob (device fingerprint, etc.); persist it so retries land on
        # the same identity.
        partial = None
        try:
            partial = client.get_settings()
        except Exception:  # noqa: BLE001
            partial = None
        return LoginOutcome(
            status=status,
            session_blob=partial,
            detail=detail,
            ig_message=ig_message,
            error_type=error_type,
            exception_name=type(exc).__name__,
        )

    # Cheap post-login probe — getting your own user_id confirms the
    # session actually works and surfaces "shadow login" failures
    # instagrapi sometimes lets through.
    try:
        client.get_timeline_feed()
    except Exception as exc:  # noqa: BLE001
        status, detail, ig_message, error_type = _classify_login_exception(exc)
        return LoginOutcome(
            status=status,
            session_blob=client.get_settings(),
            detail=f"Login accepted but probe failed. {detail}",
            ig_message=ig_message,
            error_type=error_type,
            exception_name=type(exc).__name__,
        )

    return LoginOutcome(
        status="active",
        session_blob=client.get_settings(),
        detail="ok",
    )


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


def _do_import_session_sync(
    username: str,
    password: str,
    sessionid: Optional[str],
    cookies: Optional[Dict[str, str]],
    proxy_url: Optional[str],
    existing_session: Optional[dict],
) -> LoginOutcome:
    """Bypass username/password login by reusing browser session cookies.

    The operator manually logs into instagram.com in a real browser
    (which IG already trusts because of natural usage), then exports
    cookies. We feed them to instagrapi so the scraper inherits that
    trust without ever touching the login endpoint that triggers
    bot-detection.
    """
    Client = _import_client()
    client = Client()

    if existing_session:
        try:
            client.set_settings(existing_session)
        except Exception as exc:  # noqa: BLE001
            logger.warning("instagrapi_set_settings_failed", error=str(exc), username=username)

    if proxy_url:
        try:
            client.set_proxy(proxy_url)
        except Exception as exc:  # noqa: BLE001
            return LoginOutcome(
                status="disabled",
                session_blob=None,
                detail=f"Proxy rejected: {exc}",
                exception_name=type(exc).__name__,
            )

    # Tell instagrapi who this is so future re-logins (when session
    # expires) can fall back to username/password if we choose.
    client.username = username
    client.password = password

    try:
        if sessionid:
            client.login_by_sessionid(sessionid)
        elif cookies:
            current = {}
            try:
                current = client.get_settings() or {}
            except Exception:  # noqa: BLE001
                current = {}
            current["cookies"] = cookies
            current["authorization_data"] = {
                "ds_user_id": cookies.get("ds_user_id", ""),
                "sessionid": cookies.get("sessionid", ""),
                "should_use_header_over_cookies": True,
            }
            client.set_settings(current)
        else:
            return LoginOutcome(
                status="disabled",
                session_blob=None,
                detail="Either sessionid or cookies must be provided.",
            )
    except Exception as exc:  # noqa: BLE001
        status, detail, ig_message, error_type = _classify_login_exception(exc)
        return LoginOutcome(
            status=status,
            session_blob=None,
            detail=f"Session import failed at handshake. {detail}",
            ig_message=ig_message,
            error_type=error_type,
            exception_name=type(exc).__name__,
        )

    # Multi-probe: web sessionids often work for lighter endpoints
    # (account_info / user_info_v1 / GraphQL fallback) but get rejected
    # by `feed/timeline/`. We accept any of these as "session is alive";
    # the worker will discover endpoint-specific issues at scrape time.
    user_id_int = None
    try:
        user_id_int = int(client.user_id) if client.user_id else None
    except (TypeError, ValueError):
        user_id_int = None

    probes = [
        ("account_info", lambda: client.account_info()),
    ]
    if user_id_int:
        probes.append(("user_info_v1", lambda: client.user_info_v1(user_id_int)))
        probes.append(("user_short_gql", lambda: client.user_short_gql(user_id_int)))
    probes.append(("get_timeline_feed", lambda: client.get_timeline_feed()))

    passed_probe = None
    last_error: Optional[Exception] = None
    for name, fn in probes:
        try:
            fn()
            passed_probe = name
            break
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            continue

    if passed_probe is None:
        status, detail, ig_message, error_type = _classify_login_exception(last_error)
        return LoginOutcome(
            status=status,
            session_blob=client.get_settings(),
            detail=f"Session imported but ALL probes failed. {detail}",
            ig_message=ig_message,
            error_type=error_type,
            exception_name=type(last_error).__name__ if last_error else None,
        )

    detail = f"ok (imported from browser session, probed via {passed_probe})"
    if passed_probe != "get_timeline_feed":
        detail += (
            "; warning: web cookies authenticated for lighter endpoints "
            "but mobile API (timeline) rejected. Worker may hit "
            "LoginRequired on first scrape. Common fix: warm the "
            "account from the real IG mobile app for ~24h, then retry."
        )

    return LoginOutcome(
        status="active",
        session_blob=client.get_settings(),
        detail=detail,
    )


async def import_session_from_browser(
    account: Account,
    proxy: Optional[Proxy],
    *,
    sessionid: Optional[str] = None,
    cookies: Optional[Dict[str, str]] = None,
) -> LoginOutcome:
    """Hand off browser cookies to instagrapi and run a verification probe."""
    plaintext_password = decrypt(account.password_enc)
    proxy_url = build_proxy_url(proxy)
    return await asyncio.to_thread(
        _do_import_session_sync,
        account.username,
        plaintext_password,
        sessionid,
        cookies,
        proxy_url,
        account.session_blob,
    )
