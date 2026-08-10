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
# THE VOCABULARY IS 26 LABELS: 23 cities and three buckets.
#
#     blr  hyd  pune                                    (the three legacy spellings)
#     mumbai  navi-mumbai  mumbai-suburban  thane       (the Mumbai region)
#     new-delhi  gurugram  noida                        (the NCR)
#     chennai  kolkata  ahmedabad  indore  nagpur  nashik
#     chhatrapati-sambhajinagar  coimbatore  madurai
#     hubballi  mangaluru  mysuru  belagavi
#     other-india   non-india   india-unclear           (the buckets)
#
# Matching is generous on purpose. A missed Bangalore posting is a job we never
# see; a false positive is one line of noise in a digest. When a string is
# AMBIGUOUS we bias toward india-unclear, because india-unclear can stay in a
# funnel and a confident label cannot.
#
# WHY blr / hyd / pune ARE SPELLED LIKE THAT AND THE OTHER TWENTY ARE NOT
# ----------------------------------------------------------------------
# They are older than the vocabulary and they are what `preferences.cities`
# holds on every live row, in both runtimes, plus notify.match.DEFAULT_CITIES
# and the db/schema.sql seed. Renaming them to `bengaluru` / `hyderabad` would
# have meant rewriting every account's filter inside the same transaction as the
# relabel, with a window in which a half-applied migration empties every digest —
# a real risk bought for a spelling. Leaving them alone instead makes something
# much more valuable true: THE ONLY ROWS THIS VOCABULARY CAN MOVE ARE ROWS THAT
# WERE ALREADY OUTSIDE EVERY FILTER. See the precedence note below.
#
# AMBIGUOUS IS NOT THE SAME AS "INDIAN BUT ELSEWHERE"
# ---------------------------------------------------
# That distinction used to be missing, and it leaked jobs: every India signal —
# the country name, a state, and every named Indian city — collapsed into one
# bucket that resolved to `india-unclear`, so "Lucknow" and "Remote" got the same
# label. With india-unclear in preferences.cities an InMobi "SDE 2" in Lucknow
# read as a match, and so did Noida, Mohali and Gurgaon rows: 14 of 61.
#
# `india-unclear` now means only what its name says: we could not tell.
#
# ...AND "INDIAN BUT ELSEWHERE" IS NOT THE SAME AS "NOT INDIA"
# ------------------------------------------------------------
# That was the NEXT conflation, and it lived here until the 25-city rewrite.
# `city_label` used to collapse three different verdicts into one label called
# `other`: an Indian city we had not asked for, a confidently foreign posting,
# and a string no rule recognised. One label, three facts. Which is fine while
# the only question anyone asks is "is this one of my three cities?" and fatal
# the moment somebody asks "show me everything in India" — because `other` can
# answer neither yes nor no to it.
#
# So the collapse is gone. `_classify` always computed the distinction; what
# changed is that its answer is now the stored label instead of being thrown
# away one line before it was returned. `india-elsewhere` is spelled
# `other-india` because it is a bucket a person picks from a list now, not an
# internal verdict, and `non-india` is stored as itself.
#
# PRECEDENCE, AND WHY IT IS A CORRECTNESS PROPERTY RATHER THAN A STYLE
# --------------------------------------------------------------------
# _CITY_MARKERS is an ORDERED dict and the order carries two rules:
#
#   1. blr, hyd and pune are scanned FIRST, always. Every marker they had before
#      the rewrite they still have, and no new marker was added to them that
#      changes a stored row. Those two facts together mean no string that
#      labelled blr/hyd/pune can be stolen by one of the twenty new cities, so
#      the rewrite cannot remove a job from anybody's digest, and — since no new
#      label appears in any preferences row — it cannot add one either. Verified
#      over all 1,654 distinct location strings in the database: 0 rows leaving
#      those three labels, 0 rows entering them.
#
#   2. A label whose marker CONTAINS another label's marker must be scanned
#      first. "Navi Mumbai" contains the token "mumbai"; if `mumbai` were
#      scanned first every Navi Mumbai posting would label `mumbai` and the
#      distinction the Slice-B dropdown needs would be destroyed at write time,
#      where no query can get it back. Hence navi-mumbai, mumbai-suburban and
#      thane sit above mumbai.
#
# A string naming several cities resolves by this order and not by which city it
# names first — "Mumbai / Bangalore" is blr, exactly as it has always been.
#
# THE RULE FOR ADDING A MARKER
# ----------------------------
# The canonical modern name of each of the 23 cities is always present: it is
# the label's own name, not a guess about spelling. EVERY OTHER MARKER — an old
# name, a variant, a misspelling, an airport code — is in this file only because
# scripts/survey_all_locations.py found it on a real posting, and it carries the
# row count it was found with. That rule is why the following were REJECTED:
#
#   hubli, mangalore, belgaum, sambhajinagar
#       Real alternative names, and the brief for this rewrite named three of
#       them. Zero rows in the database carry any of them. They are the exact
#       case `bengalore` / `bangalor` / `banaglore` were rejected as: cheap to
#       add, impossible to justify, and false-positive surface bought with no
#       recall. If a board ever writes one, the survey will say so.
#
#   bom, del, maa, ccu, amd, idr, nag, ixe, myq, ixg, ubl, cjb, ixm, ggn
#       Airport codes for the new cities. `blr`, `hyd` and `pnq` earned their
#       place with 178 rows of "IND-BLR-Divyasree Technopolis" and friends; no
#       board in this database writes any of these. Several are also words or
#       near-words in a location line — "del", "bom", "nag", "amd" — so they
#       would carry real risk to buy nothing.
#
# `bombay`, `calcutta` and `madras` are the deliberate exception: they match no
# row here either, but they are markers this file ALREADY HAD before the rewrite
# and deleting them would be a behaviour change made on no evidence, in the
# direction of recognising less. They moved table; they were not added.
_CITY_MARKERS: dict[str, tuple[str, ...]] = {
    # --- the three legacy labels: scanned first, contents unchanged --------
    #
    # The short codes are safe for the reason they always were: `norm` has
    # already turned every hyphen, comma and slash into a space by the time
    # `_hit` sees the string, and `_hit` anchors on word boundaries — so "blr"
    # matches inside "IND-BLR-Divyasree" (178 rows, LSEG) and cannot match
    # inside a longer word. "beng" and bare "pun" stay rejected: the first
    # occurs only inside "Bengaluru" and is likelier to be short for Bengal, and
    # PUN is not Pune's IATA code (PNQ is), so only the country-prefixed forms
    # survive and "IND-Punjab-Mohali" cannot read as Pune.
    "blr": (
        "bangalore", "bengaluru", "bangaluru", "whitefield", "electronic city",
        "electronics city", "koramangala", "bellandur", "marathahalli",
        "blr",
        # 3 rows, PhonePe, who write it this way on every posting from that
        # office. Not a word, not a prefix of one, and \b-anchored — "Bangkok",
        # the nearest trap in the data, shares only "bang" and cannot match.
        "banglore",
    ),
    "hyd": (
        "hyderabad", "secunderabad", "gachibowli", "hitec city", "hitech city",
        "hi tech city", "madhapur",
        "hyd",
        # 5 rows, Commvault, and worth being honest about: all five read
        # "Bangalore, India; Hyderbad, India[; Pune, India]", so blr outranks
        # this marker and it relabels NOTHING today. It is in anyway because it
        # is a real spelling a real board really writes, and the next posting
        # from it may not carry Bangalore alongside. That is a different case
        # from `hubli` above — this one is attested, it is merely outranked.
        "hyderbad",
    ),
    "pune": (
        "pune", "puné", "hinjewadi", "hinjawadi", "kharadi", "magarpatta", "baner",
        "pnq", "ind pun", "in pun",
    ),
    # --- superset markers: these MUST stay above `mumbai` (rule 2 above) ---
    #
    # None of the three carries a row today. They are here because they are the
    # labels' own canonical names and because the ordering they need is the kind
    # of thing that is obvious now and invisible in a year — a `thane` marker
    # added later, underneath `mumbai`, would silently never fire.
    "navi-mumbai": ("navi mumbai",),          # 1 row
    "mumbai-suburban": ("mumbai suburban",),  # 0 rows
    "thane": ("thane",),                      # 0 rows
    # --- the rest ---------------------------------------------------------
    "mumbai": ("mumbai", "bombay"),           # 103 rows / 0 rows (see above)
    # 4 rows, all bare "Delhi" ("Delhi, India", "Delhi NCR", "IN-Delhi-Remote").
    # Both spellings carry the same label because the brief's vocabulary has one
    # Delhi in it; "New Delhi" is the canonical name and "Delhi" is how boards
    # actually write it.
    "new-delhi": ("new delhi", "delhi"),
    "gurugram": ("gurugram", "gurgaon"),      # 73 rows / 15 rows
    "noida": ("noida",),                      # 271 rows — covers "Greater Noida"
    "chennai": ("chennai", "madras"),         # 44 rows / 0 rows
    "kolkata": ("kolkata", "calcutta"),       # 4 rows / 0 rows
    "ahmedabad": ("ahmedabad",),              # 4 rows
    "indore": ("indore",),                    # 1 row
    "nagpur": ("nagpur",),                    # 0 rows
    "nashik": ("nashik",),                    # 1 row, PhonePe
    # 1 row, PhonePe, spelled "Aurangabad". There are two Aurangabads and the
    # other one is in Bihar; this is a PhonePe posting on an India-wide board
    # with no state given, and the Maharashtra city is two orders of magnitude
    # the larger. Recorded here rather than argued about later.
    "chhatrapati-sambhajinagar": ("chhatrapati sambhajinagar", "aurangabad"),
    "coimbatore": ("coimbatore",),            # 2 rows
    "madurai": ("madurai",),                  # 0 rows
    "hubballi": ("hubballi",),                # 1 row, PhonePe
    "mangaluru": ("mangaluru",),              # 0 rows
    "mysuru": ("mysuru", "mysore"),           # 3 rows / 2 rows
    "belagavi": ("belagavi",),                # 0 rows
}

# Named Indian places that are NOT one of the 23. These are CONFIDENT — "Lucknow"
# is as precise about where the job is not as "London" is — and they are what the
# Slice-B "all over India" option needs in order to mean anything: a person who
# wants any Indian city has to be able to ask for the ones we never listed.
#
# Every state in this list contains NONE of the 23. A state that contains one is
# genuinely ambiguous and lives in _INDIA_VAGUE_MARKERS instead — see the note
# there, because the 3 -> 23 city change moved six states across that line.
#
# The row counts are from scripts/survey_all_locations.py over 9,689 stored jobs.
_OTHER_INDIA_MARKERS = (
    # carried over from the old _INDIA_ELSEWHERE_MARKERS, minus the fifteen
    # names that became labels of their own
    "ghaziabad", "faridabad", "gandhinagar", "surat", "jaipur", "bhopal",
    "kochi", "cochin", "trivandrum", "thiruvananthapuram", "chandigarh",
    "mohali", "lucknow", "bhubaneswar", "vizag", "visakhapatnam", "vadodara",
    "kerala", "punjab", "rajasthan", "odisha", "andhra pradesh",
    # harvested from the survey. Continental's Maharashtra plants and PhonePe's
    # small-city hiring are most of this: without these markers those postings
    # fall through to the state or the country and read as "we could not tell",
    # which is exactly the ambiguity that is NOT present when a string names a
    # town.
    "satara",        # 18 rows, Continental
    "satna",         # 4 rows, Swiggy
    "kalyani",       # 5 rows, Continental
    "nadia",         # 2 rows, Continental — the West Bengal district
    "srinagar",      # 2 rows, Swiggy
    "uttarakhand",   # 2 rows, Paytm
    "patna",         # 2 rows, razorpay + Paytm
    "bihar",         # 2 rows, PhonePe + Paytm
    "jamshedpur",    # 1 row, Continental
    "kesurdi",       # 1 row, Continental
    "kolhapur",      # 1 row, Swiggy
    "shirwal",       # 1 row, Continental
    "shivamogga",    # 1 row, PhonePe
    "bareilly",      # 1 row, PhonePe
    "purnia",        # 1 row, PhonePe
    "gwalior",       # 1 row, PhonePe
    "jalandhar",     # 1 row, PhonePe
    "karnal",        # 1 row, Paytm
    "sonepat",       # 1 row, Continental
    "rajkot",        # 1 row, PhonePe
    "chhattisgarh",  # 1 row, Paytm
    "siliguri",      # 1 row, PhonePe
    "tirupati",      # 1 row, PhonePe
    "varanasi",      # 1 row, PhonePe
    "vellore",       # 1 row, Continental
)

# India signals that name no city at all, so they cannot rule a listed city out.
#
# THE STATE LIST IS RELATIVE TO THE CITY LIST, AND THAT IS WHY IT CHANGED.
# "Is this state ambiguous?" means "does it contain a city somebody can ask
# for?", so growing the vocabulary from 3 cities to 23 moved six states across
# the line: Tamil Nadu now contains Chennai, Coimbatore and Madurai; Uttar
# Pradesh contains Noida; Gujarat contains Ahmedabad; West Bengal contains
# Kolkata; Haryana contains Gurugram; Madhya Pradesh contains Indore. Each of
# them used to be a confident `other` and each is now genuinely uncertain.
#
# CLAUDE.md's old line — "'Uttar Pradesh' cannot be [a target city], and is
# other" — was true of a three-city vocabulary and is false of this one.
#
# This only bites when the state is ALL the string says. "Kalyani, WEST BENGAL"
# names a town, so _OTHER_INDIA_MARKERS answers first and the row is precise.
#
# `ncr` IS GONE, AND IT WAS A BUG. It sat in the Indian table on the assumption
# that NCR means Delhi's National Capital Region. The Philippines has one too,
# and LSEG writes it: "Makati City, NCR, Philippines" (11 rows), "Makati, NCR,
# Philippines" (2), "Taguig, NCR, Philippines" (2). All fifteen classified as
# Indian. Under the old collapse that was invisible — they landed in `other`
# alongside genuinely foreign rows and nobody could see the difference — and the
# split is what made it visible, because they would have become `other-india`.
# Removing the marker lets `philippines` answer instead. No Indian row in the
# database says "NCR" without also saying Delhi, so nothing is lost.
_INDIA_VAGUE_MARKERS = (
    "india", "bharat",
    # states that CONTAIN at least one of the 23
    "karnataka", "maharashtra", "telangana",
    "tamil nadu", "tamilnadu", "gujarat", "haryana", "uttar pradesh",
    "west bengal", "madhya pradesh",
    "apac", "asia pacific",
)

# Anchors that place a posting outside India with enough confidence to say so.
#
# This list stopped being a filter and became a STORED LABEL in the 25-city
# rewrite, which raised its stakes: `non-india` is the one bucket an "all over
# India" reader must never see, so a foreign string that matches nothing here no
# longer falls into a shared `other` where the mistake is invisible.
#
# The second block was added from the survey, which found 590 rows across 150
# strings matching no marker in any table — almost all of them foreign places
# nobody had needed to name while everything foreign shared a bucket with
# Lucknow. Every marker below decides at least one real row; markers that were
# redundant given an earlier one were dropped rather than left in.
#
# THREE OF THEM LOOK WRONG AND ARE NOT. `norm` runs NFKD and then strips
# everything outside \w\s — and a combining accent is not a word character, so
# it becomes a SPACE rather than vanishing. "Zürich" normalises to "zu rich",
# "São Paulo" to "sa o paulo", "Québec" to "que bec". The markers are written in
# the post-`norm` form because that is the only form `_hit` will ever see. This
# is the same reason "ind pun" is spelled the way it is. Where a plain ASCII
# spelling also occurs in the data, BOTH are listed ("sao paulo" and
# "sa o paulo"), because boards are inconsistent about the accent.
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

    # --- added from the survey's 590-row residue --------------------------
    # US states. Most arrive via "Remote - California" and friends, which used
    # to reach the vague test and label india-unclear — a US remote posting
    # sitting in the one bucket that means "this might be in India".
    "california", "washington", "virginia", "maryland", "arizona", "texas",
    "utah", "ohio", "illinois", "pennsylvania", "tennessee", "mississippi",
    "oklahoma", "alaska", "connecticut", "florida", "north carolina",
    "michigan", "missouri", "new jersey",
    # US cities and regions the state name does not cover, because the board
    # abbreviated the state ("Santa Clara, CA", "Raleigh, NC", "Dallas, TX").
    # `indiana` / `indianapolis` are the one pair here attested from somewhere
    # other than the jobs table, and they earn it. CLAUDE.md's Workday section
    # records a tenant whose `locations` facet lists "Indiana - Indianapolis"
    # and friends — the reason `_hit` anchors at all, since a contains-check on
    # "india" would scope that company to the American Midwest. They matter more
    # now than they did: the no-marker fallback used to be `other`, which the
    # old funnel treated as foreign, and it is `india-unclear` now, which the
    # funnel keeps. Without these two an unscoped Workday tenant would start
    # STORING its Indiana reqs.
    "indiana", "indianapolis",
    "santa clara", "palo alto", "arlington", "dallas", "raleigh", "san mateo",
    "san diego", "columbus", "tysons", "houston", "las vegas", "miami",
    "orlando", "phoenix", "portland", "salt lake city", "tampa", "st louis",
    "redwood city", "irvine", "northeast",
    # Stripe writes its offices as bare codes and nothing else: "SF", "NYC",
    # "CHI", "NY or SF". Two letters is only safe because `_hit` anchors — and
    # a location field is the one place in this schema where "SF" cannot be an
    # abbreviation of something else.
    "nyc", "sf", "chi", "ny",
    # Databricks' and Stripe's region codes, verbatim from the board.
    "usca", "amer",
    # 1 row, Rubrik, spelled "Palo Altp, CA". A typo, attested, harmless.
    "palo altp",
    # Canada
    "british columbia", "alberta", "ontario", "calgary", "montreal", "que bec",
    # LATAM
    "sao paulo", "sa o paulo", "costa rica", "escazu", "uruguay", "cdmx",
    # EMEA. Continental's European plants are written as
    # "<town>, <region> kraj, <country>", so the country name covers the whole
    # family and the town names were dropped as redundant.
    "luxembourg", "serbia", "slovakia", "czechia", "lithuania",
    "great britain", "cork", "frankfurt", "gdynia", "perth",
    "zu rich", "mu nchen",
    # "czechia" is separate from the existing "czech" on purpose: `_hit`
    # anchors, so \bczech\b does not match "Czechia".
    # APAC
    "sri lanka", "taguig",
)

# Strings that say "somewhere / anywhere / several places" and name no city.
_VAGUE_MARKERS = (
    "remote", "anywhere", "multiple locations", "multiple", "various",
    "flexible", "hybrid", "work from home", "wfh", "global", "worldwide",
    "distributed", "any location", "all locations", "onsite", "on site",
    # Bare "locations", for Workday's locationsText when it is a COUNT and not a
    # place: "2 Locations", "3 Locations". In the live table these always arrive
    # with a city folded in from the externalPath ("India Bengaluru | 2
    # Locations"), so a city marker answers first and this never fires there —
    # but tests/data/workday_board.json carries the raw form, which reached the
    # fallback and was caught by the residue gate rather than by review. The
    # phrase markers above cover "Multiple Locations" and "All Locations" and
    # neither covers a digit.
    "locations",
    # Vendor placeholders: the field was left unfilled, which is a statement
    # about the form and not about the world. Stripe writes all four — "N/A"
    # (21 rows), "LOCATION" (2), "n/a" and "na" (1 each). They belong here
    # rather than in the residue because "the board told us nothing" is exactly
    # what india-unclear means, and because a string that reaches the fallback
    # is indistinguishable from one nobody has looked at yet.
    #
    # "n a" is the post-`norm` form of "N/A" — the slash becomes a space.
    # Bare "na" is the riskiest marker in this file: two letters, anchored, and
    # it decides exactly one row. It is admitted because a location field has no
    # English "na" in it, and it is the first thing to delete if that ever stops
    # being true.
    "n a", "na", "location",
)


def _hit(haystack: str, marker: str) -> bool:
    """Word-boundary match, for single tokens and for phrases alike.

    Boundaries stop "baner" from matching inside "banerjee", "us" from matching
    inside "australia", and "mumbai" from being found by a "mum" prefix rule
    nobody wrote.

    Phrases are anchored too, which they did not used to be. A plain substring
    test is fine for "electronic city" but not for the country-prefixed codes:
    "ind pun" as a substring fires inside "IND-Punjab-Mohali", labelling a
    Punjab posting as Pune. Anchoring the whole phrase means the trailing \\b
    has to fall after "pun", which it does in "IND-PUN-Kharadi" and does not in
    "IND-Punjab".
    """
    return re.search(rf"\b{re.escape(marker.strip())}\b", haystack) is not None


# The order the tables are consulted in. This IS the precedence, written once,
# so `explain_label` and every reader of it agree by construction rather than by
# two functions being kept in step.
#
# A named city beats a named non-city Indian place, which beats a country/state
# signal, which beats a foreign anchor, which beats plain vagueness. The
# other-india test sits ABOVE the non-India one so "Mumbai, India" reads as
# Mumbai, and BELOW the cities so "Mumbai / Bangalore" is still blr.
_NON_CITY_TABLES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("other-india", "other-india", _OTHER_INDIA_MARKERS),
    ("vague-in", "india-unclear", _INDIA_VAGUE_MARKERS),
    ("non-india", "non-india", _NON_INDIA_MARKERS),
    ("vague", "india-unclear", _VAGUE_MARKERS),
)


def explain_label(location_str: Any) -> tuple[str, str | None]:
    """(label, "table:marker") — the verdict AND the rule that produced it.

    The second element is None when NO marker in any table matched, which is the
    one case the label is a fallback rather than a decision. That difference is
    not cosmetic: `scripts/relabel_cities.py` refuses to write, and
    `scripts/survey_all_locations.py` prints an adjudication list, precisely on
    it. Two callers asking "was this decided by a rule?" get the same answer
    from the same code, instead of each replaying the tables and drifting.

    An empty location is india-unclear with the marker "<empty>" — a board that
    sent no location told us nothing, which is a fact about the board and is
    reported as such rather than as an unmatched string.
    """
    text = norm(location_str)
    if not text:
        return "india-unclear", "<empty>"

    padded = f" {text} "

    for label, markers in _CITY_MARKERS.items():
        for marker in markers:
            if _hit(padded, marker):
                return label, f"city:{marker}"

    for source, label, markers in _NON_CITY_TABLES:
        for marker in markers:
            if _hit(padded, marker):
                return label, f"{source}:{marker}"

    # Nothing matched. `india-unclear` rather than a confident bucket, because
    # "no rule recognised this" is not evidence of anything about where the job
    # is — and it is the label that keeps the posting inside a funnel, which is
    # the right direction to fail in. It used to be `other`, which quietly
    # asserted "definitely not one of your cities" about a string nobody had
    # read. The residue is meant to be EMPTY on live data; see the two scripts
    # above, which is how it is kept that way.
    return "india-unclear", None


def city_label(location_str: Any) -> str:
    """One of the 26 labels. This is what `jobs.city` stores.

    Computed at crawl time by crawler.store.upsert_seen and read — never
    recomputed — by notify.match.city_ok and its TypeScript port. The filter
    compares it to `preferences.cities` by set membership and has no opinion
    about the vocabulary, in either language, which is what let this list go
    from 5 labels to 26 without either matcher changing a line.
    """
    return explain_label(location_str)[0]


def india_funnel(location_str: Any) -> bool:
    """Could this posting plausibly be in India? Vagueness counts as yes.

    fetchers/workday.py uses this to trim an UNSCOPED board — a tenant with no
    India facet, where the alternative is paging a 2000-req worldwide board —
    and it must go on keeping the Mumbai rows it has always kept. Storing
    loosely is a promise about the jobs table; the city filter runs later and is
    where a Mumbai row is supposed to be dropped.

    DEFINED AS A COMPLEMENT, DELIBERATELY. It used to be membership of a
    hand-written frozenset of the labels that counted as Indian, which worked
    while there were five labels and became a trap at twenty-six: adding a
    twenty-fourth city and forgetting to add it to that set would make this
    function answer False for it, and the crawler would silently stop STORING
    that city — jobs vanishing before they are ever written down, which is the
    one failure `store loosely` exists to prevent and the hardest kind to
    notice. There is exactly one label this question answers no to, so that is
    the one this function names.
    """
    return explain_label(location_str)[0] != "non-india"


# Every label `city_label` can return, in the order a UI should offer them. The
# TypeScript side mirrors this in dashboard/src/lib/labels.CITY_LABELS, and
# tests/test_city_vocabulary.py asserts the two lists are identical — a label
# this side can produce that the other side rejects is a filter that silently
# matches nothing, which is the failure that list exists to prevent.
CITY_LABELS: tuple[str, ...] = (
    *_CITY_MARKERS.keys(),
    "other-india",
    "non-india",
    "india-unclear",
)


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
