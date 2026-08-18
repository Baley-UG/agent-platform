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
# The web app's own `purposeEnum`, read out of its Vue source:
# {"Game":1,"App":2,"Website":3,"Ec":4,"Account":5,"Domestic":6}. These are
# the product's top tabs, not a scale — an earlier reading here called them a
# "gradient from in-app to web inventory", which was a shadow of the real
# thing: Game peaks on game ad networks (Unity, AdColony) and Website on
# display (AdSense, X) precisely because they are different verticals.
#
# Probed for this account: 4 (Ec) and 5 (Account) are refused with
# `00:401001`; 6 (Mainland China games) is accepted but answers total 0. Only
# 1-3 are usable, so only they are offered.
PURPOSE_OPTIONS: List[Dict[str, Any]] = [
    {
        "code": 1,
        "name": "Game",
        "note": "Game advertisers. Game ad networks peak here — AdColony reports 8 732 rows "
        "against 48 under Website, Unity Ads 2 747 539.",
    },
    {
        "code": 2,
        "name": "App",
        "note": "Non-game app advertisers. The default for this deployment (AD_DEFAULT_PURPOSE).",
    },
    {
        "code": 3,
        "name": "Website",
        "note": "Website advertisers. Display and social peak here — AdSense 28 342, X 304 137 — "
        "and the media list narrows to 18 (game networks are hidden).",
    },
]

# Not offered, and why. Kept so nobody re-probes them.
PURPOSE_UNAVAILABLE: Dict[int, str] = {
    4: "Ec — hidden in the web UI; refused with 00:401001 for this account",
    5: "Account — hidden in the web UI; refused with 00:401001 for this account",
    6: "Mainland China games — accepted, but answers total 0 here",
}

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


# Network categories, from the web app's own grouping. Useful for a
# sectioned multi-select.
MEDIA_CATEGORY: Dict[str, str] = {
    "1": "Social Media",
    "2": "Social Media",
    "3": "Social Media",
    "11": "Social Media",
    "13": "Social Media",
    "16": "Social Media",
    "17": "Social Media",
    "25": "Social Media",
    "28": "Social Media",
    "29": "Social Media",
    "32": "Social Media",
    "4": "Ad Networks",
    "5": "Ad Networks",
    "6": "Ad Networks",
    "7": "Ad Networks",
    "8": "Ad Networks",
    "9": "Ad Networks",
    "10": "Ad Networks",
    "12": "Ad Networks",
    "21": "Ad Networks",
    "22": "Ad Networks",
    "23": "Ad Networks",
    "26": "Ad Networks",
    "33": "Ad Networks",
    "34": "Ad Networks",
    "14": "Local Media",
    "15": "Local Media",
    "18": "Local Media",
    "19": "Local Media",
    "30": "Local Media",
    "31": "Local Media",
}

# The web UI's one-click network groups. Worth mirroring: a caller reaching
# for "Meta Ads" otherwise has to know it means five separate codes.
MEDIA_PRESETS: List[Dict[str, Any]] = [
    {"name": "Meta Ads", "media": [2, 1, 10, 16, 32]},
    {"name": "Google Ads", "media": [4, 11, 21]},
    {"name": "TikTok for Business", "media": [13, 23, 18]},
]

# Ad placement. 302/401 exist only under purpose 1-4.
FORMAT_OPTIONS: Dict[str, str] = {
    "102": "Banner",
    "103": "Interstitial",
    "105": "Native",
    "106": "In-Feed",
    "110": "Floating",
    "301": "In-Stream Video",
    "302": "Rewarded",
    "401": "Playable",
}

# What the filter offers. Note this is a SUBSET of the `type` values a
# material can carry — see MATERIAL_TYPES.
CREATIVE_TYPE_OPTIONS: List[Dict[str, Any]] = [
    {"code": 201, "name": "Video", "group": "Video"},
    {"code": 202, "name": "Vertical Video", "group": "Video"},
    {"code": 203, "name": "Fullscreen Video", "group": "Video"},
    {"code": 102, "name": "Image", "group": "Image"},
    {"code": 104, "name": "Multiple Image", "group": "Image"},
    {"code": 103, "name": "Animated Image", "group": "Image"},
    {"code": 301, "name": "Html", "group": "Others"},
    {"code": 105, "name": "Carousel", "group": "Others"},
]

# Every value `material.type` can hold. Needed for DISPLAY, not filtering —
# our own docs previously said only "102 = image/banner, 202 = video", which
# silently mislabels 201, 203, 103, 104, 105 and 301.
MATERIAL_TYPES: Dict[str, str] = {
    "100": "Text",
    "101": "Icon",
    "102": "Image",
    "103": "Animated Image",
    "104": "Multiple Image",
    "105": "Carousel",
    "106": "Ppt",
    "201": "Video",
    "202": "Vertical Video",
    "203": "Fullscreen Video",
    "301": "Html",
}

# Upstream sort keys, with the ascending variants the UI also emits.
ORDER_OPTIONS: Dict[str, str] = {
    "max_dt_desc": "Last seen, newest first (default)",
    "max_dt": "Last seen, oldest first",
    "min_dt_desc": "First seen, newest first",
    "min_dt": "First seen, oldest first",
    "cnt_dt_desc": "Active days, most first",
    "cnt_dt": "Active days, fewest first",
    "cnt_ad_id_desc": "Related ads, most first",
    "impression_inc_2y_desc": "Impressions, highest first",
    "similar_cnt_desc": "Similar creatives, most first",
}

VIDEO_TIME_OPTIONS: Dict[str, str] = {
    "1": "under 15s",
    "2": "16-30s",
    "5": "31-45s",
    "6": "46-60s",
    "4": "over 61s",
}

MATERIAL_RATIO_OPTIONS: Dict[str, List[str]] = {
    "Horizontal": ["16:9", "5:4", "3:2", "4:3", "6:5", "2:1", "horizontal"],
    "Vertical": ["9:16", "4:5", "2:3", "3:4", "5:6", "1:2", "vertical"],
    "Square": ["1:1"],
}

APP_CASH_WAY_OPTIONS: Dict[str, str] = {
    "iap": "In-app purchase only",
    "iaa": "In-app ads only",
    "iaa_iap": "Both ads and purchases",
}

CAMPAIGN_TYPE_OPTIONS: Dict[str, str] = {
    "101": "App Store",
    "201": "Google Play",
    "299": "APK",
    "298": "RuStore (Android)",
    "295": "Galaxy Store (Android)",
}

CATEGORY_OPTIONS: Dict[str, str] = {
    "1001": "Music",
    "1002": "Social",
    "1003": "Entertainment",
    "1004": "Travel",
    "1005": "Shopping",
    "1006": "News",
    "1007": "Life",
    "1008": "Tools",
    "1009": "Educational",
    "1010": "Finance",
    "1011": "Navigation",
    "1012": "Business",
    "1013": "Health & Fitness",
    "1014": "Books & Reference",
    "1015": "Photo & Video",
    "1016": "Others",
    "1017": "Weather",
    "1018": "Sports",
    "1019": "Productivity",
    "1020": "Medical",
    "1021": "Food & Drink",
}

# Ad language (32) and voiceover language (25) are different lists — asr
# covers fewer.
LANGUAGE_OPTIONS: Dict[str, str] = {
    "af": "Afrikaans",
    "ar": "Arabic",
    "bn": "Bangla",
    "my": "Burmese",
    "zh": "Chinese (Simplified)",
    "zh-Hant": "Chinese (Traditional)",
    "nn": "Norwegian Nynorsk",
    "no": "Norwegian",
    "en": "English",
    "fr": "French",
    "de": "German",
    "hi": "Hindi",
    "km": "Khmer",
    "id": "Indonesian",
    "it": "Italian",
    "ja": "Japanese",
    "ko": "Korean",
    "ku": "Kurdish",
    "ms": "Malay",
    "mi": "Maori",
    "nl": "Dutch",
    "pt": "Portuguese",
    "ru": "Russian",
    "es": "Spanish",
    "sw": "Swahili",
    "sv": "Swedish",
    "tl": "Tagalog",
    "th": "Thai",
    "tr": "Turkish",
    "vi": "Vietnamese",
    "ur": "Urdu",
    "other": "Others",
}
ASR_LANGUAGES: List[str] = [
    "en",
    "zh",
    "vi",
    "ko",
    "pt",
    "hi",
    "ja",
    "es",
    "th",
    "de",
    "fr",
    "ar",
    "tr",
    "id",
    "it",
    "ru",
    "nl",
    "ms",
    "sv",
    "tl",
    "bn",
    "nn",
    "no",
    "sw",
    "ku",
]

# `searchDsl` entries are {key, value, type}. The web UI calls this
# `advanced` in its URL; the GraphQL variable is `searchDsl`.
SEARCH_DSL_KEYS: List[Dict[str, Any]] = [
    {"key": "appid", "label": "App ID", "types": ["equal", "notEqual"]},
    {"key": "campaign_name", "label": "App name", "types": ["contain", "exclude"]},
    {"key": "slogan", "label": "Ad description", "types": ["contain", "exclude"]},
    {"key": "asr", "label": "Voiceover text", "types": ["contain", "exclude"]},
    {"key": "brands_name", "label": "Short dramas", "types": ["contain", "exclude"]},
    {"key": "developer_name", "label": "Developer", "types": ["contain", "exclude"]},
]

# Single-value toggles the upstream accepts as 1.
BOOLEAN_FLAGS: Dict[str, str] = {
    "isNew": "Latest creatives only",
    "isNewAd": "New ads only",
    "isPre": "Pre-registration campaigns",
    "resolution": "HD creatives only",
    "asr": "Has a voiceover transcript",
    "isViolation": "Flagged as violating",
    "hasPpid": "Custom product pages",
    "postpage": "Original post",
    "hasCooperate": "Partnership ads",
    "isCreative": "Innovative creatives",
    "singleArea": "Single-country campaigns",
}

# In the web UI's URL but NOT variables of the GraphQL document, so sending
# them in `filters` does nothing at all — no error, no effect. `daterange` is
# the one that will be reached for: it is UI sugar that compiles to
# startDate/endDate.
URL_ONLY_PARAMS: Dict[str, str] = {
    "daterange": "UI sugar for a relative window; send startDate/endDate instead",
    "promotionType": "UI-only",
    "cta": "UI-only",
    "industry": "UI-only (tab-specific)",
    "gameStyle": "UI-only (tab-specific)",
    "outerPurpose": "UI-only",
    "mtype": "UI-only",
    "city": "UI-only",
    "viewType": "UI view switch, not a filter",
    "isSearchAiScene": "UI-only",
    "advanced": "the URL name for searchDsl — use searchDsl",
}


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
            "order": {
                "type": "enum",
                "options": [{"code": k, "name": v} for k, v in ORDER_OPTIONS.items()],
                "default": "max_dt_desc",
            },
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
                "options": [dict(o, category=MEDIA_CATEGORY.get(o["code"])) for o in facet("media", MEDIA_SEED)],
                "valid_codes": MEDIA_VALID_CODES,
                "presets": MEDIA_PRESETS,
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
                "options": facet("format", FORMAT_OPTIONS),
                "notes": ["Rewarded (302) and Playable (401) exist only under purpose 1-4."],
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
                "label": "Creative type",
                "options": CREATIVE_TYPE_OPTIONS,
                "notes": [
                    "These are what the FILTER offers. A material's own `type` can also be 100 Text, "
                    "101 Icon or 106 Ppt — see material_types below for the display map.",
                ],
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
                "key": "language",
                "type": "string[]",
                "label": "Ad language",
                "options": [{"code": k, "name": v} for k, v in LANGUAGE_OPTIONS.items()],
                "notes": [],
            },
            {
                "key": "category",
                "type": "int[]",
                "label": "Store category",
                "options": [{"code": k, "name": v} for k, v in CATEGORY_OPTIONS.items()],
                "notes": [],
            },
            {
                "key": "campaignType",
                "type": "int[]",
                "label": "Promotion platform",
                "options": [{"code": k, "name": v} for k, v in CAMPAIGN_TYPE_OPTIONS.items()],
                "notes": [],
            },
            {
                "key": "appCashWay",
                "type": "string[]",
                "label": "Monetization",
                "options": [{"code": k, "name": v} for k, v in APP_CASH_WAY_OPTIONS.items()],
                "notes": [],
            },
            {
                "key": "videoTime",
                "type": "int",
                "label": "Video duration",
                "options": [{"code": k, "name": v} for k, v in VIDEO_TIME_OPTIONS.items()],
                "notes": ["A bucket, not seconds."],
            },
            {
                "key": "materialRatio",
                "type": "string[]",
                "label": "Aspect ratio",
                "options": [
                    {"code": r, "name": r, "group": g} for g, rs in MATERIAL_RATIO_OPTIONS.items() for r in rs
                ],
                "notes": [],
            },
            {
                "key": "asrLanguage",
                "type": "string",
                "label": "Voiceover language",
                "options": [{"code": c, "name": LANGUAGE_OPTIONS.get(c, c)} for c in ASR_LANGUAGES],
                "notes": ["Shorter list than `language` — 25 versus 32."],
            },
            {
                "key": "minDuration",
                "type": "int",
                "label": "Ad days, minimum",
                "notes": ["Days the ad has been running, not video length."],
            },
            {"key": "maxDuration", "type": "int", "label": "Ad days, maximum", "notes": []},
            {
                "key": "ageRange",
                "type": "string[]",
                "label": "Target age",
                "notes": [],
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
        "flags": {
            "type": "int",
            "note": "Send 1 to enable. Absent means no constraint.",
            "options": [{"key": k, "name": v} for k, v in BOOLEAN_FLAGS.items()],
        },
        "search_dsl_keys": SEARCH_DSL_KEYS,
        "material_types": MATERIAL_TYPES,
        "purpose_unavailable": PURPOSE_UNAVAILABLE,
        "url_only_params": {
            "note": "Present in the web UI's URL but NOT variables of the GraphQL document. "
            "Sending them in `filters` does nothing — no error, no effect.",
            "params": URL_ONLY_PARAMS,
        },
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
