"""Normalisation shared by every fetcher.

The one shape
-------------
Every fetcher, for every ATS, returns a list of dicts with EXACTLY these keys:

    {"native_id", "title", "location", "url", "posted_at", "department"}

`posted_at` is an ISO-8601 string or None. Everything else is a string or None.
A missing vendor field becomes None — never a crash, never an invented value.

This module deliberately applies NO judgment about which jobs matter. It labels
a location; it does not decide whether that label is interesting.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any, Iterable

JOB_KEYS = ("native_id", "title", "location", "url", "posted_at", "department")

_PUNCT = re.compile(r"[^\w\s]", flags=re.UNICODE)
_WS = re.compile(r"\s+")


def norm(s: Any) -> str:
    """Lowercase, strip punctuation, collapse whitespace.

    Deliberately does NOT strip seniority words: "Engineer" and "Senior
    Engineer" are different jobs and must produce different canonical ids.
    """
    if s is None:
        return ""
    text = unicodedata.normalize("NFKD", str(s))
    text = _PUNCT.sub(" ", text.lower())
    return _WS.sub(" ", text).strip()


def canonical_id(
    ats: str,
    company_id: Any,
    native_id: Any,
    title: Any = "",
    location: Any = "",
) -> str:
    """Stable identity for a posting, used later as the jobs table primary key.

    Preferred form is the vendor's own id: `{ats}:{company_id}:{native_id}`.
    Boards that expose no id (scraped JSON-LD, sitemaps) fall back to a hash of
    company + normalised title + normalised location, which is stable as long as
    the posting's own text is stable.
    """
    if native_id is not None and str(native_id).strip():
        return f"{ats}:{company_id}:{str(native_id).strip()}"
    seed = f"{company_id}|{norm(title)}|{norm(location)}"
    return "hash:" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]


# --- city labelling -------------------------------------------------------
#
# Matching is generous on purpose. A missed Bangalore posting is a job we never
# see; a false positive is one line of noise in a digest. When a string is
# ambiguous we bias toward india-unclear rather than other, because
# india-unclear stays in the funnel and other drops out of it.

_CITY_MARKERS: dict[str, tuple[str, ...]] = {
    "blr": (
        "bangalore", "bengaluru", "bangaluru", "whitefield", "electronic city",
        "electronics city", "koramangala", "bellandur", "marathahalli",
    ),
    "hyd": (
        "hyderabad", "secunderabad", "gachibowli", "hitec city", "hitech city",
        "hi tech city", "madhapur",
    ),
    "pune": ("pune", "puné", "hinjewadi", "hinjawadi", "kharadi", "magarpatta", "baner"),
}

# Elsewhere in India: real postings, just not our three cities.
_INDIA_MARKERS = (
    "india", "bharat",
    "mumbai", "bombay", "navi mumbai", "thane", "delhi", "new delhi", "ncr",
    "gurgaon", "gurugram", "noida", "ghaziabad", "faridabad", "chennai",
    "madras", "kolkata", "calcutta", "ahmedabad", "gandhinagar", "surat",
    "jaipur", "indore", "bhopal", "nagpur", "coimbatore", "kochi", "cochin",
    "trivandrum", "thiruvananthapuram", "chandigarh", "mohali", "lucknow",
    "bhubaneswar", "vizag", "visakhapatnam", "mysore", "mysuru", "vadodara",
    "karnataka", "maharashtra", "telangana", "andhra pradesh", "tamil nadu",
    "kerala", "gujarat", "haryana", "uttar pradesh", "west bengal", "punjab",
    "rajasthan", "odisha", "apac", "asia pacific",
)

# Anchors that place a posting outside India with enough confidence to say so.
_NON_INDIA_MARKERS = (
    "united states", "usa", " us ", "u s a", "canada", "mexico", "brazil",
    "argentina", "colombia", "chile", "peru", "united kingdom", "uk", "england",
    "london", "manchester", "scotland", "ireland", "dublin", "france", "paris",
    "germany", "berlin", "munich", "hamburg", "spain", "madrid", "barcelona",
    "portugal", "lisbon", "netherlands", "amsterdam", "belgium", "brussels",
    "switzerland", "zurich", "geneva", "austria", "vienna", "poland", "warsaw",
    "krakow", "romania", "bucharest", "czech", "prague", "hungary", "budapest",
    "sweden", "stockholm", "norway", "oslo", "denmark", "copenhagen", "finland",
    "helsinki", "italy", "rome", "milan", "greece", "athens", "turkey",
    "istanbul", "israel", "tel aviv", "uae", "dubai", "abu dhabi", "qatar",
    "doha", "saudi", "riyadh", "egypt", "cairo", "nigeria", "lagos", "kenya",
    "nairobi", "south africa", "cape town", "johannesburg", "australia",
    "sydney", "melbourne", "new zealand", "auckland", "singapore", "malaysia",
    "kuala lumpur", "indonesia", "jakarta", "thailand", "bangkok", "vietnam",
    "hanoi", "philippines", "manila", "japan", "tokyo", "osaka", "china",
    "beijing", "shanghai", "shenzhen", "hong kong", "taiwan", "taipei",
    "korea", "seoul", "san francisco", "new york", "seattle", "austin",
    "boston", "chicago", "denver", "atlanta", "los angeles", "san jose",
    "toronto", "vancouver", "emea", "latam", "americas", "europe",
)

# Strings that say "somewhere / anywhere / several places" and name no city.
_VAGUE_MARKERS = (
    "remote", "anywhere", "multiple locations", "multiple", "various",
    "flexible", "hybrid", "work from home", "wfh", "global", "worldwide",
    "distributed", "any location", "all locations", "onsite", "on site",
)


def _hit(haystack: str, marker: str) -> bool:
    """Word-boundary match for single tokens, substring for phrases.

    Boundaries stop "baner" from matching inside "banerjee" and " us " from
    matching inside "australia"; phrases like "electronic city" are safe as
    plain substrings.
    """
    marker = marker.strip()
    if " " in marker:
        return marker in haystack
    return re.search(rf"\b{re.escape(marker)}\b", haystack) is not None


def city_label(location_str: Any) -> str:
    """One of: blr | pune | hyd | india-unclear | other.

    Precedence: a named target city wins; then any other India signal; then a
    confident non-India anchor; then vagueness, which resolves to india-unclear.
    An empty location is india-unclear — an unlabelled posting on an Indian
    company's board is far more likely to be Indian than not.
    """
    text = norm(location_str)
    if not text:
        return "india-unclear"

    padded = f" {text} "

    for label in ("blr", "hyd", "pune"):
        if any(_hit(padded, m) for m in _CITY_MARKERS[label]):
            return label

    if any(_hit(padded, m) for m in _INDIA_MARKERS):
        return "india-unclear"

    if any(_hit(padded, m) for m in _NON_INDIA_MARKERS):
        return "other"

    if any(_hit(padded, m) for m in _VAGUE_MARKERS):
        return "india-unclear"

    return "other"


# --- small helpers used by the fetchers -----------------------------------


def iso_or_none(value: Any) -> str | None:
    """Coerce whatever a vendor calls a date into an ISO-8601 string, or None.

    Handles ISO strings (with or without a trailing Z), epoch seconds and epoch
    milliseconds (Lever), and Recruitee's "YYYY-MM-DD HH:MM:SS UTC". Anything
    unrecognised returns None rather than a guess.
    """
    if value is None or value == "":
        return None

    if isinstance(value, (int, float)):
        seconds = value / 1000.0 if value > 1e11 else float(value)
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return None

    text = str(value).strip()
    if not text:
        return None

    candidate = text.replace("Z", "+00:00")
    if candidate.endswith(" UTC"):
        candidate = candidate[:-4] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
        # A date-only vendor field parses naive; assume UTC so the value can go
        # into a TIMESTAMPTZ column later without an implicit-timezone surprise.
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.isoformat()
    except ValueError:
        pass

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            continue
    return None


def clean_str(value: Any) -> str | None:
    """Trim a vendor string; empty and whitespace-only become None."""
    if value is None:
        return None
    text = _WS.sub(" ", str(value)).strip()
    return text or None


def join_parts(parts: Iterable[Any], sep: str = ", ") -> str | None:
    """Join the non-empty pieces of a structured address into one raw string."""
    seen: list[str] = []
    for part in parts:
        text = clean_str(part)
        if text and text.lower() not in {s.lower() for s in seen}:
            seen.append(text)
    return sep.join(seen) or None


def make_job(
    native_id: Any,
    title: Any,
    location: Any,
    url: Any,
    posted_at: Any,
    department: Any,
) -> dict[str, Any]:
    """Build the one canonical row shape. The only place these keys are set."""
    return {
        "native_id": clean_str(native_id),
        "title": clean_str(title),
        "location": clean_str(location),
        "url": clean_str(url),
        "posted_at": iso_or_none(posted_at),
        "department": clean_str(department),
    }
