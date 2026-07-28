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
# AMBIGUOUS we bias toward india-unclear rather than other, because
# india-unclear stays in the funnel and other drops out of it.
#
# AMBIGUOUS IS NOT THE SAME AS "INDIAN BUT ELSEWHERE"
# ---------------------------------------------------
# That distinction used to be missing, and it leaked jobs. Every India signal —
# the country name, a state, and every named Indian city — collapsed into one
# bucket that resolved to `india-unclear`. So "Lucknow" and "Remote" got the
# same label, even though one of them is a confident statement that the posting
# is NOT in Bangalore, Hyderabad or Pune and the other is no statement at all.
# With india-unclear in preferences.cities, an InMobi "SDE 2" posted in Lucknow
# read as a match; so did Noida, Mohali and Gurgaon rows.
#
# `india-unclear` now means only what its name says: we could not tell. A string
# that NAMES an Indian place we do not want is as certain as a string naming
# London, and it labels `other` for the same reason — the uncertainty the
# inclusion bias exists to protect is simply not present.
#
# The three home states are the deliberate exception and stay unclear:
# "Karnataka" alone may well be Bengaluru, "Maharashtra" may be Pune,
# "Telangana" may be Hyderabad. A state that CONTAINS a target city is genuinely
# ambiguous, which is exactly the case the unclear label is for.

# SHORT CODES, AND WHY THEY ARE SAFE HERE
# ---------------------------------------
# Some boards write the airport/office code instead of the city name. LSEG's
# Workday tenant is one: every Bangalore posting on it reads
# "IND-BLR-Divyasree Technopolis", which named none of the markers below and so
# labelled `other` — a label the default preferences drop. 135 real Bengaluru
# jobs were being discarded by a string format.
#
# Codes are ADDITIVE; every full name above still matches, and precedence is
# unchanged (blr -> hyd -> pune), so a string carrying both a code and a name
# resolves exactly as it did before.
#
# The reason a three-letter token is not reckless here is `norm`: it has already
# turned every hyphen, comma, slash and parenthesis into a space by the time
# _hit sees the string, and _hit anchors single tokens with \b. So "blr" matches
# the BLR in "IND-BLR-Divyasree" and the one in "Global BLR team", and cannot
# match inside a longer word.
#
# Two candidates were deliberately REJECTED rather than added:
#
#   "beng"  is not a code any board uses for Bangalore. In the data it occurs
#           only inside "Bengaluru", which the full name already catches, so it
#           buys no recall — while "Beng" standing alone is likelier to be
#           short for Bengal. Adding it would be pure false-positive surface.
#
#   "pun"   bare is an English word, and PUN is not Pune's IATA code — PNQ is.
#           It appears in this database only inside "Pune"/"PUNE", again already
#           matched by name. So the bare token is out and only the
#           country-prefixed forms survive, which cannot be a sentence's "pun".

# MISSPELLINGS, AND WHY THERE IS ONLY ONE
# ---------------------------------------
# A recruiter typing the city name wrong drops the posting into `other` just as
# thoroughly as an office code does. PhonePe's Greenhouse board writes
# "Banglore" — no second 'a' — on every posting from that office.
#
# Which spellings are worth a marker is a question about the data, not about
# English, so it was answered from the data: scripts/survey_locations.py prints
# every distinct location string in the `other` and `india-unclear` buckets, and
# a fuzzy pass compared every 4+ letter token in all 1197 stored rows against
# "bangalore", "bengaluru", "hyderabad", "pune" and "secunderabad". Across the
# whole table exactly ONE near-miss exists:
#
#     banglore   3 rows, PhonePe, all labelled `other`
#
# The fuzzy pass's only other hit was "bengal", inside "Kolkata, West Bengal" —
# a real state, correctly not Bangalore, and the same string names Bangalore
# elsewhere so the row already labelled blr. Hyderabad and Pune are spelled
# correctly on every board in this database; neither gets a misspelling marker,
# because a marker that matches nothing is false-positive surface bought with no
# recall. That is the same test "beng" and bare "pun" failed above.
#
# Deliberately NOT added, having been checked against the data first:
#
#   "bangalore urban"   the district form. Already matches — it CONTAINS the
#   "bengaluru urban"   standalone token "bangalore"/"bengaluru", and _hit
#                       anchors on word boundaries rather than on the whole
#                       string, so the existing full names cover both. A
#                       separate marker would be dead weight.
#
#   "bengalore"         plausible spellings that appear nowhere in this database
#   "bangalor"          or in tests/data. Cheap to add and impossible to justify:
#   "banaglore"         the rule for adding a marker here is that a real posting
#                       was lost to it, and no posting was.
#
# What makes "banglore" safe is what makes "blr" safe: it is not a word, not a
# prefix of one, and \b-anchored. "Bangkok" — 3 LSEG rows, the nearest thing in
# the data to a trap — shares only "bang" and cannot match.
_CITY_MARKERS: dict[str, tuple[str, ...]] = {
    "blr": (
        "bangalore", "bengaluru", "bangaluru", "whitefield", "electronic city",
        "electronics city", "koramangala", "bellandur", "marathahalli",
        "blr",
        "banglore",
    ),
    "hyd": (
        "hyderabad", "secunderabad", "gachibowli", "hitec city", "hitech city",
        "hi tech city", "madhapur",
        "hyd",
    ),
    "pune": (
        "pune", "puné", "hinjewadi", "hinjawadi", "kharadi", "magarpatta", "baner",
        # PNQ is unambiguous on its own. "pun" is only trusted behind a country
        # prefix — and only because _hit anchors the whole phrase, so "ind pun"
        # matches "IND-PUN-Kharadi" and NOT "IND-Punjab-Mohali".
        "pnq", "ind pun", "in pun",
    ),
}

# Elsewhere in India: real postings, in a named place that is definitely not
# Bangalore, Hyderabad or Pune. These are CONFIDENT — "Lucknow" is as precise
# about where the job is not as "London" is — so they label `other`, and only
# their India-ness (used by the Workday volume fallback, below) is remembered.
#
# Every state here excludes all three target cities. The three that do not —
# Karnataka, Maharashtra, Telangana — are in _INDIA_VAGUE_MARKERS instead.
_INDIA_ELSEWHERE_MARKERS = (
    "mumbai", "bombay", "navi mumbai", "thane", "delhi", "new delhi", "ncr",
    "gurgaon", "gurugram", "noida", "ghaziabad", "faridabad", "chennai",
    "madras", "kolkata", "calcutta", "ahmedabad", "gandhinagar", "surat",
    "jaipur", "indore", "bhopal", "nagpur", "coimbatore", "kochi", "cochin",
    "trivandrum", "thiruvananthapuram", "chandigarh", "mohali", "lucknow",
    "bhubaneswar", "vizag", "visakhapatnam", "mysore", "mysuru", "vadodara",
    "andhra pradesh", "tamil nadu", "kerala", "gujarat", "haryana",
    "uttar pradesh", "west bengal", "punjab", "rajasthan", "odisha",
)

# India signals that name no city at all, so they cannot rule a target city out.
# "India" is the country; the three states each CONTAIN one of our cities; APAC
# is a sales region. All of them are genuine uncertainty, which is the one thing
# `india-unclear` is for.
_INDIA_VAGUE_MARKERS = (
    "india", "bharat",
    "karnataka", "maharashtra", "telangana",
    "apac", "asia pacific",
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
    "istanbul", "israel", "tel aviv", "uae", "united arab emirates", "dubai",
    # "bay area" is here for the same reason "banglore" is a blr marker: a real
    # row was mislabelled by it. samsara posts "Remote - SF Bay Area", which
    # named no marker at all and fell through to the vague test as though the
    # location were unknown. It is not unknown; it is California.
    "bay area", "abu dhabi", "qatar",
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
    """Word-boundary match, for single tokens and for phrases alike.

    Boundaries stop "baner" from matching inside "banerjee" and "us" from
    matching inside "australia".

    Phrases are anchored too, which they did not used to be. A plain substring
    test is fine for "electronic city" but not for the country-prefixed codes:
    "ind pun" as a substring fires inside "IND-Punjab-Mohali", labelling a
    Punjab posting as Pune. Anchoring the whole phrase means the trailing \\b
    has to fall after "pun", which it does in "IND-PUN-Kharadi" and does not in
    "IND-Punjab". Verified to change no label across every location string in
    the database and in tests/data — it only closes the hole.
    """
    return re.search(rf"\b{re.escape(marker.strip())}\b", haystack) is not None


# The finer verdict city_label collapses. Six values rather than five, because
# two questions are being asked of the same string and they have different
# answers: "does this job sit in one of my cities?" (city_label, which is what
# jobs.city stores and the filter reads) and "is this posting plausibly in India
# at all?" (india_funnel, which is what the Workday volume fallback needs).
#
# `india-elsewhere` and `non-india` both collapse to `other` for the first
# question and part company on the second. Keeping them apart HERE rather than
# giving jobs.city a sixth value is deliberate: the stored label is read by two
# runtimes and a preferences editor, and a label nothing filters on would be a
# vocabulary change for every one of them to buy one call site a predicate.
_INDIA_FUNNEL = frozenset({"blr", "hyd", "pune", "india-elsewhere", "india-unclear"})


def _classify(location_str: Any) -> str:
    """blr | hyd | pune | india-elsewhere | india-unclear | non-india | unknown.

    Precedence: a named target city wins; then a named Indian place that is not
    one of them; then a country/state/region signal that names no city; then a
    confident non-India anchor; then vagueness, which resolves to india-unclear.
    An empty location is india-unclear — an unlabelled posting on an Indian
    company's board is far more likely to be Indian than not.

    The elsewhere test sits ABOVE the non-India one so "Mumbai, India" is read
    as Mumbai, and BELOW the target cities so "Mumbai / Bangalore" is still blr.
    """
    text = norm(location_str)
    if not text:
        return "india-unclear"

    padded = f" {text} "

    for label in ("blr", "hyd", "pune"):
        if any(_hit(padded, m) for m in _CITY_MARKERS[label]):
            return label

    if any(_hit(padded, m) for m in _INDIA_ELSEWHERE_MARKERS):
        return "india-elsewhere"

    if any(_hit(padded, m) for m in _INDIA_VAGUE_MARKERS):
        return "india-unclear"

    if any(_hit(padded, m) for m in _NON_INDIA_MARKERS):
        return "non-india"

    if any(_hit(padded, m) for m in _VAGUE_MARKERS):
        return "india-unclear"

    return "unknown"


def city_label(location_str: Any) -> str:
    """One of: blr | pune | hyd | india-unclear | other.

    `other` is every posting we are confident is not in one of the three cities,
    whether it says so by naming Lucknow or by naming London. `india-unclear` is
    reserved for the strings that make no such statement — "Remote", "India",
    "Multiple Locations", a state that contains a target city, and the empty
    string. That split is the whole point: the inclusion bias protects genuine
    uncertainty, and a posting that names its city is not uncertain.
    """
    verdict = _classify(location_str)
    return verdict if verdict in ("blr", "hyd", "pune", "india-unclear") else "other"


def india_funnel(location_str: Any) -> bool:
    """Could this posting plausibly be in India? Vagueness counts as yes.

    Not the same question as `city_label(...) != "other"`, and the difference is
    exactly the rows that changed meaning when `india-elsewhere` stopped being
    called `india-unclear`. fetchers/workday.py uses this to trim an UNSCOPED
    board — a tenant with no India facet, where the alternative is paging a
    2000-req worldwide board — and it must go on keeping the Mumbai rows it has
    always kept. Storing loosely is a promise about the jobs table; the city
    filter runs later and is where a Mumbai row is supposed to be dropped.
    """
    return _classify(location_str) in _INDIA_FUNNEL


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
