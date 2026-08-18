"""One machine-readable description of every filter the panel can offer.

Why this exists: the vocabulary was living in two places. The panel carried
a hand-written `staticOptions.ts` while the service carried the measured
truth, and they drifted — the panel's list was missing ironSource, Vungle,
Chartboost, AdSense, Mintegral, Pangle, Moloco, Bigo and InMobi, and it
asserted four networks (X, Pinterest, Snapchat, VKontakte) that no
measurement here has ever confirmed. A form built from this endpoint cannot
drift, because the drift has nowhere to live.

Two sources are merged per facet:

* **Observed** — `ad_dimensions`, populated from ingested payloads, carrying
  usage counts so a panel can sort by "actually has data".
* **Seeded** — the small measured tables below, so a fresh database still
  offers a usable form on day one. Observed values always win; a seed only
  fills a gap.

Nothing speculative belongs in the seeds. Every name here came back from the
live endpoint in a `material.media[]` / `platform[]` / `format[]` payload.
Codes that are *valid* but whose name we have never seen are published
separately as `valid_codes`, so a panel can offer a raw-code input for them
instead of pretending they do not exist.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlmodel import Session

from app.core.config import settings
from app.schemas.jobs import KNOWN_ORDERS
from app.services import queries

# ---------------------------------------------------------------------------
# Seeds — measured, never guessed
# ---------------------------------------------------------------------------

# `purpose` is the only required filter. Labels are measured, not from any
# upstream documentation: sampling each corpus shows the advertiser-type mix
# is what separates them. See README, "What the three purpose values
# actually are".
PURPOSE_OPTIONS: List[Dict[str, Any]] = [
    {
        "code": 1,
        "name": "App advertisers",
        "note": "In-app network inventory. AdColony reports 8 732 rows here against 48 under "
        "purpose 3; Unity Ads peaks here too. Use this for competitor app creatives.",
    },
    {
        "code": 2,
        "name": "Broader mix",
        "note": "Between the two. Still app-advertiser dominant, but admits far more display "
        "inventory than 1 — AdSense goes from 1 007 rows to 27 744.",
    },
    {
        "code": 3,
        "name": "Web, social and display",
        "note": "Web and social networks peak here — AdSense 28 342, X 304 137 — and in-app "
        "networks fall away. The Meta family is ~20x denser than under purpose 1.",
    },
]

# Networks seen in live payloads. Codes without a name are NOT listed here —
# they go out as `valid_codes`.
MEDIA_SEED: Dict[str, str] = {
    "1": "Instagram",
    "2": "Facebook",
    "3": "X",
    "4": "AdMob",
    "5": "Unity Ads",
    "6": "ironSource",
    "7": "AdColony",
    "8": "AppLovin",
    "9": "Vungle",
    "10": "Facebook (FAN)",
    "11": "YouTube",
    "12": "Chartboost",
    "13": "TikTok",
    "14": "Yahoo Japan",
    "15": "Line Japan",
    "16": "Messenger",
    "17": "Pinterest",
    "18": "TopBuzz Japan",
    "19": "SmartNews Japan",
    "21": "AdSense",
    "22": "Mintegral",
    "23": "Pangle",
    "25": "Snapchat",
    "26": "Moloco",
    "28": "Kwai",
    "29": "SnackVideo",
    "30": "Yandex",
    "31": "VKontakte",
    "32": "Threads",
    "33": "Bigo Ads",
    "34": "InMobi",
}

PLATFORM_SEED: Dict[str, str] = {"1": "Android", "2": "iOS"}

FORMAT_SEED: Dict[str, str] = {
    "103": "Interstitial",
    "106": "In-Feed",
    "302": "Rewarded",
}

# The upstream accepts these and rejects everything else with "Parameter
# error" — one bad code fails the WHOLE request, so a panel must not offer a
# code outside this set. Measured by probing every value.
# Enumerated one code at a time against the live endpoint, because an
# invalid code fails the WHOLE request with `00:401001` rather than being
# ignored. Rejected: 20, 24, 27, 35, 36. Note 32 (Threads) IS valid — an
# earlier note here recorded the top of the range as 31, which would have had
# this endpoint tell a panel not to offer Threads while listing it as an
# option.
MEDIA_VALID_CODES: List[int] = list(range(1, 20)) + list(range(21, 24)) + [25, 26] + list(range(28, 35))


def _merge(
    observed: List[Dict[str, Any]],
    seed: Dict[str, str],
) -> List[Dict[str, Any]]:
    """Observed values first (they carry counts), then unseen seeds."""
    options: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for row in observed:
        code = str(row["code"])
        seen.add(code)
        options.append(
            {
                "code": code,
                "name": row.get("name") or seed.get(code),
                "icon": row.get("icon"),
                "materials": row.get("material_count", 0),
                "source": "observed",
            }
        )
    for code, name in seed.items():
        if code in seen:
            continue
        options.append({"code": code, "name": name, "icon": None, "materials": 0, "source": "seed"})
    # Named first, then by usage, then by code — a form wants the useful ones
    # at the top and stable ordering for the rest.
    options.sort(key=lambda o: (o["name"] is None, -(o["materials"] or 0), o["code"]))
    return options


def build(session: Session) -> Dict[str, Any]:
    """Assemble the filter schema, merging observed facets over the seeds."""
    observed: Dict[str, List[Dict[str, Any]]] = {}
    for row in queries.list_dimensions(session, kind=None, limit=5000):
        observed.setdefault(row["kind"], []).append(row)

    def facet(kind: str, seed: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
        return _merge(observed.get(kind, []), seed or {})

    return {
        "job": {
            "page_from": {"type": "int", "min": 1, "default": 1},
            "page_to": {
                "type": "int",
                "min": 1,
                "max": settings.AD_MAX_PAGE,
                "default": settings.AD_DEFAULT_PAGE_TO,
                "note": f"page x limit is capped at {settings.max_rows_per_filter_set} rows per filter set "
                f"({settings.AD_MAX_PAGE} pages x {settings.AD_PAGE_SIZE}). Past the end the upstream "
                "REPEATS the last page instead of returning empty, so a job stops when "
                "page * limit >= total.",
            },
            "order": {"type": "enum", "options": list(KNOWN_ORDERS), "default": "max_dt_desc"},
            "mirror": {
                "type": "bool|null",
                "default": None,
                "note": "null follows AD_MIRROR_MEDIA; true/false overrides it for this job.",
            },
            "app_id": {
                "type": "string",
                "note": "Store app id. Compiles into searchDsl — do NOT put it in `campaign`, "
                "which accepts a numeric id and silently returns zero rows.",
            },
            "rows_per_page": settings.AD_PAGE_SIZE,
            "max_rows_per_filter_set": settings.max_rows_per_filter_set,
        },
        "filters": [
            {
                "key": "purpose",
                "type": "int",
                "required": False,
                "default": settings.AD_DEFAULT_PURPOSE,
                "options": PURPOSE_OPTIONS,
                "notes": [
                    "Required by the upstream, but this service fills in the default above when "
                    "you omit it — a form does not need a control for it.",
                    "Not nested: the same TikTok/TR filter answers 1.43M / 1.95M / 1.67M for 1 / 2 / 3, "
                    "so a higher number is not a wider net.",
                ],
            },
            {
                "key": "media",
                "type": "int[]",
                "label": "Network",
                "options": facet("media", MEDIA_SEED),
                "valid_codes": MEDIA_VALID_CODES,
                "notes": [
                    "OR within the list. A creative matches if ANY of its networks is selected.",
                    "Rows report the creative's WHOLE network set, not the part you asked for — "
                    "filtering TikTok returns rows that also list Pangle and Instagram.",
                    "A small network disappears inside a big filter: TikTok is 2.1% of a Google+Meta "
                    "union, and page 1 of max_dt_desc had none of it. Filter to one network to see it.",
                    "One invalid code fails the WHOLE request, so never offer a code outside valid_codes.",
                ],
            },
            {
                "key": "area",
                "type": "string[]",
                "label": "Country",
                "options": facet("area"),
                "pattern": "^[A-Z]{2}$",
                "notes": [
                    "Uppercase ISO-3166-1 alpha-2, ONE code per array element.",
                    'Joining codes into one element ("TR;SA") is rejected — do not string-join a ' "multi-select.",
                ],
            },
            {
                "key": "platform",
                "type": "int[]",
                "label": "OS",
                "options": facet("platform", PLATFORM_SEED),
                "notes": ["The upstream's `platform` facet is the operating system, not the network."],
            },
            {
                "key": "format",
                "type": "int[]",
                "label": "Ad format",
                "options": facet("format", FORMAT_SEED),
                "notes": [],
            },
            {
                "key": "channel",
                "type": "int[]",
                "label": "Channel",
                "options": facet("channel"),
                "notes": ["Read-only facet: the upstream accepts no `channel` filter, so use it for display."],
            },
            {
                "key": "creativeType",
                "type": "int[]",
                "options": [{"code": 102, "name": "Image / banner"}, {"code": 202, "name": "Video"}],
                "notes": [],
            },
            {
                "key": "isAllDate",
                "type": "int",
                "options": [{"code": 1, "name": "All time"}, {"code": 0, "name": "Use the date range"}],
                "notes": [
                    "isAllDate: 1 OVERRIDES startDate/endDate.",
                    "Omitting both isAllDate and a range is NOT 'everything' — it applies a default "
                    "recent window (18M rows versus 205M on purpose 2).",
                ],
            },
            {
                "key": "startDate",
                "type": "date",
                "notes": ["YYYY-MM-DD. Needs endDate and isAllDate unset or 0."],
            },
            {"key": "endDate", "type": "date", "notes": ["YYYY-MM-DD."]},
            {
                "key": "keyword",
                "type": "string",
                "notes": ["Lenient — an unmatched keyword returns zero rows rather than an error."],
            },
            {
                "key": "gender",
                "type": "int[]",
                "notes": ["Lenient: unknown values are ignored rather than rejected."],
            },
            {
                "key": "searchDsl",
                "type": "object[]",
                "notes": [
                    "The web UI's advanced panel. Entries are {key, value, type}; key 'appid' targets a "
                    "store app id. Prefer the job-level `app_id`, which builds this correctly.",
                ],
            },
        ],
        "rejected_keys": {
            "keys": ["page", "order"],
            "note": "The worker owns paging and ordering; sending them inside `filters` is a 422 because "
            "they would silently contradict page_from/page_to.",
        },
        "read": {
            "sort": {
                "options": sorted(queries.SORT_OPTIONS),
                "default": queries.DEFAULT_SORT,
                "note": "For GET /materials. An unknown value is a 400, not a silent fallback.",
            }
        },
    }
