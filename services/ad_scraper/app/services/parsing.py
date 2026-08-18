"""Pure parsers for the quirks of the AppGrowing payload.

Every function here is total: it takes whatever the API sent (including
None, empty string and garbage) and returns either a clean value or None.
No exceptions escape — a single odd field must never fail an ingestion
run, and these are the fields most likely to drift.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

# "1.1M" / "476.3K" / "2.4B" / "1,234" / "930" — and the bounded forms the
# platform uses at the top of its range: ">10M", "<1K", "~5M", "10M+".
# Those matter MORE than the exact ones: a creative reported as ">10M" is by
# definition among the best performers, and leaving it unparsed would NULL
# `impression_inc_2y` and drop it out of every threshold and sort.
_COMPACT_RE = re.compile(r"^\s*[><~≈]?\s*([0-9][0-9,._\s]*?)\s*([KMBT])?\s*\+?\s*$", re.IGNORECASE)
_SUFFIX_MULTIPLIER = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000, "T": 1_000_000_000_000}


def parse_compact_number(value: Any) -> Optional[int]:
    """Turn the API's display-formatted counts into an integer.

    The `impression_inc_2y` field arrives pre-formatted for humans
    ("1.1M", "476.3K") which sorts alphabetically and therefore wrongly.
    We keep the original string on the row for display and store this
    parsed value for ordering and thresholds.

    Precision note: "1.1M" really is all the API gives us, so the parsed
    value is 1_100_000 — accurate to the significant digits provided, not
    to the true impression count. Bounded forms are read as their stated
    bound (">10M" → 10_000_000), which understates the true figure; the
    original string stays in `impression_inc_2y_raw` so a UI can still show
    "> 10M" rather than a flat 10M.

        >>> parse_compact_number("1.1M")
        1100000
        >>> parse_compact_number("476.3K")
        476300
        >>> parse_compact_number(">10M")
        10000000
        >>> parse_compact_number("930")
        930
        >>> parse_compact_number("") is None
        True
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)

    match = _COMPACT_RE.match(str(value))
    if not match:
        return None

    digits, suffix = match.group(1), match.group(2)
    # Thousands separators and stray whitespace are noise; a '.' is a real
    # decimal point in this format ("476.3K"), so it survives.
    digits = digits.replace(",", "").replace("_", "").replace(" ", "")
    try:
        number = float(digits)
    except ValueError:
        return None

    if suffix:
        number *= _SUFFIX_MULTIPLIER[suffix.upper()]
    return int(round(number))


def parse_int(value: Any) -> Optional[int]:
    """Coerce the API's string-typed integers (`duration`, `similar_cnt`)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def parse_date(value: Any) -> Optional[date]:
    """Parse the API's `YYYY-MM-DD` date strings."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def expires_at_from_auth_key(url: Optional[str]) -> Optional[datetime]:
    """Extract the expiry timestamp from a signed YouCloud CDN URL.

    The CDN signs URLs as `?auth_key=<epoch>-<nonce>-<uid>-<md5>`, where
    the leading epoch is when the signature stops being accepted. Knowing
    it lets the panel tell "this link is dead, use the S3 mirror" without
    issuing a request.

        >>> expires_at_from_auth_key(
        ...     "https://cdn.example/x.mp4?auth_key=1788334146-abc-0-def"
        ... ).isoformat()
        '2026-09-02T07:29:06+00:00'
        >>> expires_at_from_auth_key("https://cdn.example/x.mp4") is None
        True
    """
    if not url:
        return None
    try:
        query = parse_qs(urlparse(str(url)).query)
    except ValueError:
        return None

    raw = (query.get("auth_key") or [None])[0]
    if not raw:
        return None

    head = str(raw).split("-", 1)[0]
    if not head.isdigit():
        return None
    try:
        return datetime.fromtimestamp(int(head), tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def jwt_expires_at(token: Optional[str]) -> Optional[datetime]:
    """Read the `exp` claim of a JWT WITHOUT verifying its signature.

    We are not authenticating the token — the YouCloud server does that.
    We only want to know when it will stop being accepted so the worker
    can refresh ahead of time. Decoding the payload segment is enough and
    avoids taking a JWT library dependency for one integer.
    """
    if not token:
        return None
    parts = str(token).split(".")
    if len(parts) < 2:
        return None

    import base64
    import json

    payload_b64 = parts[1]
    # JWT uses base64url without padding; add the padding back.
    payload_b64 += "=" * (-len(payload_b64) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(payload_b64.encode("ascii")))
    except (ValueError, TypeError, UnicodeEncodeError):
        return None
    if not isinstance(payload, dict):
        return None

    exp = payload.get("exp")
    if not isinstance(exp, (int, float)) or isinstance(exp, bool):
        return None
    try:
        return datetime.fromtimestamp(int(exp), tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def filename_from_url(url: Optional[str], fallback_ext: str = ".bin") -> str:
    """Derive a safe object-key filename from a signed CDN URL.

    The query string (which carries the signature) is dropped — otherwise
    every re-mirror of the same file would land under a different key.
    """
    import os

    name = ""
    if url:
        try:
            name = os.path.basename(urlparse(str(url)).path)
        except ValueError:
            name = ""
    if not name:
        name = f"asset{fallback_ext}"
    if "." not in name:
        name = f"{name}{fallback_ext}"
    return name[:80]
