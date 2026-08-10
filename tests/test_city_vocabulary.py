"""The 26-label vocabulary: what it must never break, and what it now decides.

This file is the 25-city rewrite's own test, and it is organised around the two
promises that made the rewrite safe to run against 9,689 live rows:

  1. NOTHING THAT MATCHED STOPS MATCHING. `blr`, `hyd` and `pune` keep their
     spellings and their first place in the precedence order, so no string that
     labelled one of them can be stolen by one of the twenty new cities.
  2. NOTHING NEW STARTS MATCHING. Twenty new labels appear in no preferences row
     — the seed is still exactly ('blr','pune','hyd') — so a row moving between
     the other labels cannot reach an inbox.

Together those two are why `npm run match-parity` returns the same total before
and after the relabel. The frozen corpus below is the first one as data.

Every string in this file is a REAL location string from the database, found by
scripts/survey_all_locations.py over all 9,689 rows. That is the same discipline
the marker table itself is built on: a test written from imagination proves the
labeller agrees with whoever wrote the test.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fetchers.normalize import (  # noqa: E402
    CITY_LABELS,
    city_label,
    explain_label,
    india_funnel,
)
from notify.match import DEFAULT_CITIES, city_ok  # noqa: E402

PREFS_CITIES = set(DEFAULT_CITIES)


# --- 1. the frozen corpus --------------------------------------------------
#
# Real strings that labelled blr / hyd / pune BEFORE the rewrite, one per shape
# the boards actually write, weighted toward the ones carrying the most rows.
# If any of these moves, a job left somebody's digest.

FROZEN_BLR = [
    "Bangalore",                                   # 212 rows
    "Bengaluru, India",                            # 203
    "Bangalore, India",                            # 153
    "IND-BLR-Divyasree Technopolis",               # 152 — the office-code form
    "India, Bengaluru",                            # 147
    "Bengaluru",                                   # 106
    "BANGALORE",                                   # 85
    "Bengaluru, KA, India",                        # 85
    "Bangalore, Karnataka",                        # 66
    "India - Bangalore",                           # 53
    "Bangalore, IND",                              # 43
    "IND-Bangalore-A, RMZ Infinity",               # 41
    "Bengaluru, , India",                          # 33
    "India Bengaluru | 2 Locations",               # 31
    "Bangalore | 2 Locations",                     # 29
    "IND BLR Divyasree Technopolis | 2 Locations", # 24
    "Hybrid in Bangalore, India",                  # 20
    "IND-Bangalore-TowerE,RMZ Infin",              # 20
    "Bengaluru - BLR1",                            # 9
    "Bengaluru-VTP, India",                        # 8
    "IN-Bangalore-MSO",                            # 8
    "BANGALORE 05",                                # 7
    "Remote, Bangalore",                           # 6
    "Bengaluru; Gurugram",                         # 5 — a new city is also named
    "bengaluru",                                   # 5
    "Electronic City, Bengaluru, Karnataka, India",# 4
    "Banglore",                                    # 3 — the PhonePe misspelling
    "Bengaluru Urban, KA, India",                  # 1 — the district form
    "Bangalore,India",                             # 1 — no space after the comma
    "IN - Bengaluru",                              # 1
]

# Every one of these names a NEW city alongside Bangalore. They are the whole
# point of rule 1: under a table ordered any other way, each would move.
FROZEN_BLR_MULTI = [
    "Mumbai, Maharashtra | Bangalore, Karnataka",                  # 7 rows
    "Bangalore, IND; Mohali, IND",                                 # 6
    "Bengaluru, India; Mumbai, India",                             # 6
    "Noida, Uttar Pradesh | Bangalore, Karnataka",                 # 6
    "Bengaluru; Gurugram; Mumbai",                                 # 2
    "Mumbai / Bangalore",                                          # 2
    "Bangalore, India; Mumbai, India; New Delhi, India",           # 1
    "Delhi, Delhi | Bangalore, Karnataka | Kolkata, West Bengal",  # 1
    "Bangalore, Karnataka | Mumbai, Maharashtra | Hyderabad, Telangana | Noida, Uttar Pradesh",
    "Noida, Uttar Pradesh | Gurugram, Haryana | Bangalore, Karnataka | Mumbai, Maharashtra",
    "Bangalore, IND; Gurgaon, IND; Mumbai, IND",                   # 1
    "Bangalore, India; Hyderbad, India; Pune, India",              # 3
]

FROZEN_HYD = [
    "Hyderabad, Telangana, India",            # 81 rows
    "India Hyderabad | 2 Locations",          # 57
    "India - Hyderabad",                      # 38
    "Hyderabad, India",                       # 20
    "IND-Hyderabad-CapitaLand",               # 14
    "Hyderabad",                              # 12
    "Hyderabad, , India",                     # 11
    "HYDERABAD",                              # 6
    "hyderabad",                              # 4
    "Hyderabad, IND",                         # 3
    "HYDERABAD 04",                           # 1
    "INVECAS HYDERABAD",                      # 1
    # 1 row, and it names four cities of which three are newly labellable.
    "Noida, Uttar Pradesh | Ahmedabad, Indore, Chennai , Hyderabad",
]

FROZEN_PUNE = [
    "India, Pune",                                              # 30 rows
    "Pune, Maharashtra, India",                                 # 18
    "Pune, Maharashtra",                                        # 16
    "PUNE 05",                                                  # 12
    "IN-Pune",                                                  # 11
    "PUNE",                                                     # 4
    "Pune",                                                     # 4
    "India Pune | 2 Locations",                                 # 3
    "Pune, IND",                                                # 3
    "Ahmedabad, Gujarat | Pune, Maharashtra",                   # 1
    "Mumbai, Maharashtra | Pune, Maharashtra",                  # 1
    "Pune / satara, Maharashtra, India",                        # 1 — satara is a marker now
    "Pune, MH, India",                                          # 1
    "Pune, Maharashtra | Chennai, Tamil Nadu | Trivandrum, Kerala",  # 1
]


@pytest.mark.parametrize("location", FROZEN_BLR + FROZEN_BLR_MULTI)
def test_every_blr_string_in_the_database_still_labels_blr(location):
    assert city_label(location) == "blr"


@pytest.mark.parametrize("location", FROZEN_HYD)
def test_every_hyd_string_in_the_database_still_labels_hyd(location):
    assert city_label(location) == "hyd"


@pytest.mark.parametrize("location", FROZEN_PUNE)
def test_every_pune_string_in_the_database_still_labels_pune(location):
    assert city_label(location) == "pune"


@pytest.mark.parametrize(
    "location", FROZEN_BLR + FROZEN_BLR_MULTI + FROZEN_HYD + FROZEN_PUNE
)
def test_the_frozen_corpus_still_passes_the_operator_gate(location):
    """The label is the mechanism; THIS is the promise — the job still arrives."""
    assert city_ok({"city": city_label(location)}, _prefs())


def _prefs():
    from notify.match import Preferences

    return Preferences()


def test_the_default_gate_is_still_exactly_three_cities():
    """The rewrite must not have touched the filter. It is the other half of why
    twenty new labels cannot reach an inbox: nothing selects them."""
    assert DEFAULT_CITIES == ("blr", "pune", "hyd")


@pytest.mark.parametrize(
    "label", [l for l in CITY_LABELS if l not in ("blr", "pune", "hyd")]
)
def test_no_new_label_passes_the_operator_gate(label):
    assert not city_ok({"city": label}, _prefs())


# --- 2. superset precedence ------------------------------------------------
#
# "Navi Mumbai" CONTAINS the token "mumbai". The ordering in _CITY_MARKERS is
# what stops the more specific label being swallowed, and ordering is exactly
# the kind of invariant that survives review and dies to a later alphabetical
# tidy-up.


@pytest.mark.parametrize(
    "location,expected",
    [
        ("Navi Mumbai, Maharashtra", "navi-mumbai"),
        ("Navi Mumbai", "navi-mumbai"),
        ("Vashi, Navi Mumbai", "navi-mumbai"),
        ("Mumbai Suburban", "mumbai-suburban"),
        ("Mumbai Suburban, Maharashtra", "mumbai-suburban"),
        ("Thane", "thane"),
        ("Thane, Maharashtra", "thane"),
        ("Mumbai", "mumbai"),
        ("Mumbai, Maharashtra", "mumbai"),
    ],
)
def test_the_specific_mumbai_labels_are_not_swallowed_by_mumbai(location, expected):
    assert city_label(location) == expected


def test_new_delhi_and_delhi_are_one_label():
    """The brief's vocabulary has one Delhi in it; boards write both spellings."""
    assert city_label("New Delhi") == "new-delhi"
    assert city_label("Delhi, India") == "new-delhi"
    assert city_label("IN-Delhi-Remote") == "new-delhi"
    assert city_label("Delhi NCR") == "new-delhi"


def test_a_multi_city_string_resolves_by_table_order_not_by_position():
    """Unchanged in kind from before the rewrite, and worth pinning: the answer
    does not depend on which city the recruiter typed first."""
    assert city_label("Mumbai / Bangalore") == "blr"
    assert city_label("Bangalore / Mumbai") == "blr"
    assert city_label("Chennai, Hyderabad") == "hyd"
    assert city_label("Noida | Pune | 2 Locations") == "pune"


# --- 3. the other-split ----------------------------------------------------


@pytest.mark.parametrize(
    "location",
    [
        "Lucknow",                    # 8 rows — one of the original 14 leaked
        "Mohali, IND",                # 7
        "Kalyani, WEST BENGAL, India",# 5
        "Faridabad, HR, India",       # 5
        "Jaipur, Rajasthan",          # 4
        "Satna, MP, India",           # 4
        "Surat, Gujarat",             # 4
        "Satara , Maharashtra , India",  # 18 across spellings, Continental
        "Vadodara",                   # 3
        "Ghaziabad, Uttar Pradesh",   # 1
        "Patna",                      # 1
        "Vellore, Tamil Nadu",        # 1
        "Bhubaneswar, Odisha",        # 1
        "Chandigarh",                 # 1
        "Kochi (Kerala)",             # 1
        "Vizag, Andhra Pradesh",      # 1
    ],
)
def test_an_indian_place_outside_the_23_is_other_india(location):
    assert city_label(location) == "other-india"
    assert india_funnel(location) is True


@pytest.mark.parametrize(
    "location",
    [
        "Santa Clara, California",    # 105 rows — the biggest residue string
        "Mountain View, California",  # 44
        "Bellevue, Washington",       # 34
        "Washington, DC",             # 25
        "Palo Alto, CA",              # 21
        "Belgrade, Serbia",           # 12
        "Colombo Sri Lanka | 2 Locations",  # 11
        "Púchov, Trenčiansky kraj, Slovakia (Slovak Republic)",  # 10
        "Luxembourg",                 # 8
        "Zürich, CH",                 # 5
        "São Paulo",                  # 6
        "Costa Rica",                 # 6
        "Otrokovice 2, Zlínský kraj, Czechia",  # 2
        "Sergeičikai I, Kauno apskr., Lithuania",  # 5
        "Landeshauptstadt München, DEU",  # 3
        "AU-Perth-WW",                # 2
        "Remote - SF Bay Area",       # the samsara row
        "USCA",                       # 4
        "NYC-Privy",                  # 5
    ],
)
def test_a_foreign_place_is_non_india(location):
    assert city_label(location) == "non-india"
    assert india_funnel(location) is False


def test_the_philippines_ncr_rows_are_not_indian():
    """`ncr` used to sit in the Indian table and matched the Philippines' own
    National Capital Region: 15 LSEG rows classified as Indian. Under the old
    collapse they hid inside `other` alongside genuinely foreign rows; the split
    is what would have turned them into `other-india` and made the bug visible."""
    for location in (
        "Makati City, NCR, Philippines",
        "Makati, NCR, Philippines",
        "Taguig, NCR, Philippines",
        "PHL Taguig City CitiPlaza | 2 Locations",
    ):
        assert city_label(location) == "non-india", location
        assert india_funnel(location) is False, location


@pytest.mark.parametrize(
    "location",
    [
        "India",                    # 23 rows
        "Remote",                   # 19
        "Remote India",             # 10
        "India - Remote",           # 5
        "Multiple Locations",
        "All Locations",
        "Karnataka",                # a state that contains a listed city
        "Maharashtra",
        "Telangana",
        "Tamil Nadu",               # newly ambiguous: contains Chennai
        "Uttar Pradesh",            # newly ambiguous: contains Noida
        "Gujarat",                  # contains Ahmedabad
        "West Bengal",              # contains Kolkata
        "Haryana",                  # contains Gurugram
        "Madhya Pradesh",           # contains Indore
        "N/A",                      # 21 rows, Stripe's placeholder
        "LOCATION",                 # 2 rows
        "",
        None,
    ],
)
def test_a_string_that_names_no_city_is_india_unclear(location):
    assert city_label(location) == "india-unclear"


def test_the_six_states_that_changed_meaning_are_recorded_as_such():
    """"Is this state ambiguous?" means "does it contain a city somebody can ask
    for?", so growing the vocabulary moved six states across the line. Every one
    of them was `other` before and is `india-unclear` now — and both are outside
    the operator's filter, which is why the change costs no inbox anything."""
    for state in (
        "Tamil Nadu", "Uttar Pradesh", "Gujarat",
        "West Bengal", "Haryana", "Madhya Pradesh",
    ):
        assert city_label(state) == "india-unclear", state
        assert not city_ok({"city": city_label(state)}, _prefs()), state


def test_a_named_town_beats_the_state_it_sits_in():
    """The state flip above only bites when the state is ALL the string says."""
    assert city_label("Kalyani, WEST BENGAL, India") == "other-india"
    assert city_label("Vellore, Tamil Nadu") == "other-india"
    assert city_label("Ghaziabad, Uttar Pradesh") == "other-india"
    assert city_label("Kolkata, WEST BENGAL, India") == "kolkata"
    assert city_label("Coimbatore, Tamil Nadu") == "coimbatore"


# --- 4. decoys for the new markers -----------------------------------------
#
# Same discipline the `blr` and `banglore` markers were held to: a short or
# word-like marker earns its place only if it cannot fire inside something else.


@pytest.mark.parametrize(
    "location,not_label",
    [
        ("Thanet, Kent, England", "thane"),
        ("Bhatinda, Punjab", "thane"),
        ("Indiana, United States", "indore"),
        ("Indianapolis", "indore"),
        ("Indonesia", "indore"),
        ("Nagpurwala Street", "nagpur"),
        ("Chennaiyin FC Stadium", "chennai"),
        ("Mumbaikar Cafe", "mumbai"),
        ("Noidaville", "noida"),
    ],
)
def test_markers_do_not_fire_inside_longer_words(location, not_label):
    assert city_label(location) != not_label


def test_india_is_not_found_inside_indiana():
    """The oldest trap in this file, and the reason `_hit` anchors: Workday
    tenants list "Indiana - Indianapolis" and a contains-check would scope a
    company to Indiana, USA."""
    assert city_label("Indiana - Indianapolis") == "non-india"
    assert city_label("Indiana") == "non-india"


@pytest.mark.parametrize(
    "location",
    ["Mumbai", "New Delhi", "Chennai", "Kolkata", "Thane", "Indore", "Nashik"],
)
def test_the_canonical_names_are_matched_as_whole_words(location):
    """The mirror of the decoys: anchoring must not have broken the plain case."""
    assert city_label(location) in CITY_LABELS
    assert city_label(location) not in ("other-india", "non-india", "india-unclear")


def test_the_rejected_aliases_are_still_rejected():
    """hubli / mangalore / belgaum are real alternative names and the brief for
    this rewrite named three of them. No row in the database carries any, so
    they are out, exactly as `bengalore` and `bangalor` are — the rule for a
    marker is that a real posting was lost to it."""
    assert city_label("Hubli") != "hubballi"
    assert city_label("Mangalore") != "mangaluru"
    assert city_label("Belgaum") != "belagavi"
    # ...and the canonical spellings, which ARE markers, still work.
    assert city_label("Hubballi") == "hubballi"
    assert city_label("Mangaluru") == "mangaluru"
    assert city_label("Belagavi") == "belagavi"


def test_no_airport_code_for_a_new_city_is_a_marker():
    """`blr`, `hyd` and `pnq` earned their place with real rows. None of these
    appear on any board here, and several are words or near-words."""
    for code, label in (
        ("BOM", "mumbai"), ("DEL", "new-delhi"), ("MAA", "chennai"),
        ("CCU", "kolkata"), ("AMD", "ahmedabad"), ("IDR", "indore"),
        ("NAG", "nagpur"), ("IXE", "mangaluru"), ("MYQ", "mysuru"),
    ):
        assert city_label(code) != label, code


def test_the_evidenced_typos_are_markers_and_the_unevidenced_ones_are_not():
    # attested by the survey
    assert city_label("Banglore") == "blr"          # 3 rows, PhonePe
    assert city_label("Hyderbad") == "hyd"          # 5 rows, Commvault
    assert city_label("Palo Altp, CA") == "non-india"  # 1 row, Rubrik
    # not attested anywhere, and therefore not markers
    for absent in ("bengalore", "bangalor", "banaglore", "hydrabad", "puna"):
        assert city_label(absent) not in ("blr", "hyd", "pune"), absent


def test_belgium_and_bengal_are_not_near_misses():
    """The fuzzy pass proposed both. Belgium is a country and Bengal is a
    region, and neither is a misspelling of Belagavi or Bengaluru."""
    assert city_label("Belgium") == "non-india"
    assert city_label("West Bengal") == "india-unclear"
    assert city_label("Bengaluru") == "blr"


# --- 5. the funnel ---------------------------------------------------------


@pytest.mark.parametrize("label", CITY_LABELS)
def test_india_funnel_says_no_to_exactly_one_label(label):
    """Defined as a complement so a 24th city cannot be forgotten from it. If
    this ever needs a list, the Workday unscoped trim starts silently DROPPING
    a city's postings before they are stored — jobs vanishing before they are
    written down, which is the hardest failure here to notice."""
    sample = {
        "blr": "Bangalore", "hyd": "Hyderabad", "pune": "Pune",
        "mumbai": "Mumbai", "navi-mumbai": "Navi Mumbai",
        "mumbai-suburban": "Mumbai Suburban", "thane": "Thane",
        "new-delhi": "New Delhi", "gurugram": "Gurugram", "noida": "Noida",
        "chennai": "Chennai", "kolkata": "Kolkata", "ahmedabad": "Ahmedabad",
        "indore": "Indore", "nagpur": "Nagpur", "nashik": "Nashik",
        "chhatrapati-sambhajinagar": "Chhatrapati Sambhajinagar",
        "coimbatore": "Coimbatore", "madurai": "Madurai",
        "hubballi": "Hubballi", "mangaluru": "Mangaluru", "mysuru": "Mysuru",
        "belagavi": "Belagavi", "other-india": "Lucknow",
        "non-india": "London", "india-unclear": "India",
    }[label]
    assert city_label(sample) == label
    assert india_funnel(sample) is (label != "non-india")


def test_the_workday_fallback_still_keeps_the_mumbai_rows():
    """The one call site: fetchers/workday.py trims an UNSCOPED tenant's board
    before storing it, and must go on keeping everything plausibly Indian."""
    for location in ("Mumbai", "Mumbai, India", "Lucknow", "India", "Remote", "N/A", ""):
        assert india_funnel(location) is True, location
    for location in ("London", "Santa Clara, California", "Makati City, NCR, Philippines"):
        assert india_funnel(location) is False, location


# --- 6. the residue --------------------------------------------------------


def test_explain_label_reports_the_marker_that_decided():
    """Both the survey and the relabel gate on this, so it is worth pinning."""
    assert explain_label("IND-BLR-Divyasree Technopolis") == ("blr", "city:blr")
    assert explain_label("Lucknow")[1] == "other-india:lucknow"
    assert explain_label("Santa Clara, California")[1] == "non-india:california"
    assert explain_label("")[1] == "<empty>"


def test_a_string_no_rule_recognises_falls_back_rather_than_asserting():
    """The fallback is india-unclear and not a confident bucket: "no rule
    matched" is not evidence about where a job is. It used to be `other`, which
    quietly asserted "definitely not one of your cities" about a string nobody
    had read."""
    label, marker = explain_label("Qwertyville, Zzyzx Province")
    assert marker is None
    assert label == "india-unclear"


def test_no_string_in_the_fixtures_reaches_the_fallback():
    """The residue is meant to be empty on real data. tests/data is the only
    corpus available offline, so it is the one asserted here; the live gate is
    scripts/relabel_cities.py, which refuses to apply on a non-empty residue."""
    import json

    unmatched = []
    for path in (Path(__file__).parent / "data").glob("*.json"):
        blob = json.loads(path.read_text(encoding="utf-8"))
        for location in _locations(blob):
            if explain_label(location)[1] is None:
                unmatched.append((path.name, location))
    assert not unmatched, f"strings with no marker: {unmatched}"


def _locations(blob):
    """Every string under any key that looks like a location, at any depth."""
    if isinstance(blob, dict):
        for key, value in blob.items():
            if isinstance(value, str) and "location" in key.lower():
                yield value
            else:
                yield from _locations(value)
    elif isinstance(blob, list):
        for item in blob:
            yield from _locations(item)


# --- 7. the two runtimes agree about the vocabulary ------------------------


def test_the_typescript_city_labels_are_the_same_list_in_the_same_order():
    """The runtimes share no code, so this list exists twice. A label Python can
    produce that TypeScript rejects is a filter the API refuses to save; a label
    TypeScript offers that Python never produces is a checkbox matching nothing,
    forever, silently. Same argument tests/test_default_preferences.py makes
    about the seed."""
    source = (
        Path(__file__).resolve().parent.parent
        / "dashboard" / "src" / "lib" / "labels.ts"
    ).read_text(encoding="utf-8")

    block = re.search(r"export const CITY_LABELS = \[(.*?)\] as const;", source, re.S)
    assert block, "could not find CITY_LABELS in labels.ts"
    ts_labels = tuple(re.findall(r'"([^"]+)"', block.group(1)))

    assert ts_labels == CITY_LABELS


def test_the_typescript_side_no_longer_offers_other():
    """`other` meant three things at once and none of them survive. A preferences
    row still carrying it would match zero rows."""
    source = (
        Path(__file__).resolve().parent.parent
        / "dashboard" / "src" / "lib" / "labels.ts"
    ).read_text(encoding="utf-8")
    block = re.search(r"export const CITY_LABELS = \[(.*?)\] as const;", source, re.S)
    assert '"other"' not in block.group(1)
    assert "other" not in CITY_LABELS
