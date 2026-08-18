"""Error taxonomy for the YouCloud GraphQL endpoint.

**The endpoint answers HTTP 200 for everything.** An expired session, an
insufficient plan, a rejected filter and a successful query all come back
`200 OK`; the difference lives in the body:

    {"errors":[{"message":null,"extensions":{
        "c":"05:403001",
        "m":"Login session has expired. Please log in again.",
        "code":400, "silent":false, "traceId":"..."}}],
     "data":{"materialList":null}}

So `response.raise_for_status()` is worse than useless here — it would
wave an expired session through as a success with a null payload, and the
worker would record "0 materials found" instead of "credentials dead".
Classification has to read the body. That is what this module does.

`extensions.c` is a `NN:NNNNNN` pair. The leading group is the family and
is the stable part; the trailing digits vary within a family. Observed:

    05:400001  malformed / unparseable sessionId
    05:403001  valid-looking sessionId the server no longer accepts
    00:403001  no session at all → "Permission denied, please upgrade your plan"

We therefore branch on the family (`05:` → session problem) rather than on
exact codes, so a third session-related code doesn't get misfiled as an
unknown error. `extensions.m` is a localised, user-facing sentence — fine
for logs, never a control-flow input except for the two cases below where
the platform sends no distinguishing code at all.

One non-JSON case exists: omitting the `accept-language` header yields
HTTP 406 with the plain-text body `The Language: [] is no acceptable`.
The client always sends the header; `TransportError` covers it if a proxy
ever strips it.
"""

from __future__ import annotations

from typing import Any, Optional

# `extensions.c` family prefixes.
_SESSION_FAMILY = "05:"
_PLAN_DENIED_CODE = "00:403001"

# The platform sends no distinguishing code for these two, only a
# sentence. Matched case-insensitively on a fragment, so minor wording
# changes ("please clear the filters") still land correctly.
_BAD_FILTER_FRAGMENT = "parameter error"
_TRANSIENT_FRAGMENT = "system is busy"


class YouCloudError(Exception):
    """Base for every YouCloud failure.

    Carries the platform's own diagnostics so the job row can record them
    verbatim (`ad_scrape_jobs.error_code` / `.error`).
    """

    def __init__(self, message: str, *, code: Optional[str] = None, trace_id: Optional[str] = None) -> None:
        """Record the message plus the platform's own code and trace id."""
        super().__init__(message)
        self.code = code
        self.trace_id = trace_id

    def __str__(self) -> str:  # noqa: D105
        base = super().__str__()
        if self.code:
            return f"[{self.code}] {base}"
        return base


class AuthExpired(YouCloudError):
    """The session cookie is gone, malformed, or no longer accepted.

    Recoverable: refresh the session, retry the request once.
    """


class PlanDenied(YouCloudError):
    """The account's plan does not cover this query.

    Terminal — retrying costs time and changes nothing. Also what an
    entirely unauthenticated request gets.
    """


class BadFilter(YouCloudError):
    """The filter set or page number was rejected.

    Terminal. The common cause is `page > 200`; the client refuses those
    before they leave the process.
    """


class TransientError(YouCloudError):
    """Server-side hiccup ("The system is busy"). Retry with backoff."""


class TransportError(YouCloudError):
    """Network failure, timeout, non-JSON body, or an unexpected status.

    Retryable — the request may never have reached the resolver.
    """


def _first_error(payload: Any) -> Optional[dict]:
    """Return the first entry of a GraphQL `errors` array, if any."""
    if not isinstance(payload, dict):
        return None
    errors = payload.get("errors")
    if not isinstance(errors, list) or not errors:
        return None
    first = errors[0]
    return first if isinstance(first, dict) else {}


def classify(payload: Any) -> Optional[YouCloudError]:
    """Map a decoded GraphQL response onto an exception instance.

    Returns None when the payload carries no `errors` array — i.e. the
    caller may read `data`. Does NOT raise; the caller decides, because
    the retry policy differs per call site.
    """
    first = _first_error(payload)
    if first is None:
        return None

    extensions = first.get("extensions") if isinstance(first.get("extensions"), dict) else {}
    code = extensions.get("c")
    code = str(code) if code else None
    trace_id = extensions.get("traceId")
    trace_id = str(trace_id) if trace_id else None

    # `message` is reliably null on this endpoint; `extensions.m` holds the
    # human-readable text. Fall back through both so we never log "None".
    message = extensions.get("m") or first.get("message") or "unknown YouCloud error"
    message = str(message)
    lowered = message.lower()

    kwargs = {"code": code, "trace_id": trace_id}

    if code and code.startswith(_SESSION_FAMILY):
        return AuthExpired(message, **kwargs)
    if code == _PLAN_DENIED_CODE:
        return PlanDenied(message, **kwargs)
    if _BAD_FILTER_FRAGMENT in lowered:
        return BadFilter(message, **kwargs)
    if _TRANSIENT_FRAGMENT in lowered:
        return TransientError(message, **kwargs)

    # Unknown error family. Treated as transient rather than terminal:
    # a genuinely permanent unknown error will still exhaust its retries
    # and fail the job, whereas classifying it terminal would give up on
    # a recoverable blip we have not catalogued yet.
    return TransientError(message, **kwargs)


def metric_label(exc: BaseException) -> str:
    """Prometheus-safe label for an error.

    Prefers the platform's own code so dashboards group by the thing the
    platform actually distinguishes; falls back to the exception class for
    transport-level problems that never got a code.
    """
    code = getattr(exc, "code", None)
    if code:
        return str(code)
    return type(exc).__name__
