"""city_label's short-code matching, and the false positives it must not make.

Some boards write an airport/office code rather than a city name. LSEG's Workday
tenant writes every Bangalore posting as "IND-BLR-Divyasree Technopolis"; before
codes were recognised those 135 rows labelled `other`, which the default
preferences drop, so they never reached a match or a digest.

The risk a three-letter token carries is obvious, so half of this file is decoys.
Two things keep it safe and both are asserted here:

    norm() has already replaced every hyphen, comma, slash and parenthesis with
    a space before _hit runs, so a code inside "IND-BLR-..." is a standalone
    token by the time it is matched.

    _hit anchors with \\b, for phrases as well as single tokens, so a code can
    never match inside a longer word.
"""

from __future__ import annotations

import pytest

from fetchers.normalize import city_label


# --- the strings this change exists for -----------------------------------


@pytest.mark.parametrize(
    "location",
    [
        "IND-BLR-Divyasree Technopolis",          # 121 LSEG rows
        "IND BLR Divyasree Technopolis | 2 Locations",  # 12 more
        "IND BLR Divyasree Technopolis | 3 Locations",  # and 2 more
    ],
)
def test_the_lseg_strings_that_were_being_dropped_now_label_blr(location):
    assert city_label(location) == "blr"


@pytest.mark.parametrize(
    "location,expected",
    [
        ("BLR", "blr"),
        ("blr", "blr"),
        ("Global BLR team", "blr"),
        ("IND-BLR", "blr"),
        ("IN-BLR-Whitefield", "blr"),
        ("BLR/HYD", "blr"),          # precedence: blr wins, as before
        ("HYD", "hyd"),
        ("IND-HYD-CapitaLand", "hyd"),
        ("IND/HYD", "hyd"),
        ("PNQ", "pune"),
        ("IND-PNQ-Kharadi", "pune"),
        ("IND-PUN-Hinjewadi", "pune"),
        ("IN-PUN", "pune"),
    ],
)
def test_codes_are_matched_as_delimited_tokens(location, expected):
    assert city_label(location) == expected


# --- the decoys -----------------------------------------------------------


@pytest.mark.parametrize(
    "location",
    [
        "Assembler",          # the brief's decoy
        "Assembler, Remote",
        "Cabler",
        "Tumblr HQ",          # contains "blr" flanked by word characters
        "blrx",
        "xblr",
        "ablra",
    ],
)
def test_blr_never_fires_inside_a_longer_word(location):
    assert city_label(location) != "blr"


@pytest.mark.parametrize(
    "location",
    [
        "Punjab",
        "IND-Punjab-Mohali",   # the phrase-anchoring case: "ind pun" + "jab"
        "IN-Punjab",
        "Punjab, India",
        "Punta Arenas, Chile",
    ],
)
def test_punjab_and_friends_never_label_pune(location):
    assert city_label(location) != "pune"


@pytest.mark.parametrize(
    "location",
    ["Hydraulic Engineer", "Hydrabad", "hydro plant", "hyde park", "Hydra"],
)
def test_hyd_never_fires_inside_a_longer_word(location):
    assert city_label(location) != "hyd"


def test_the_full_name_is_still_anchored_too():
    """`Hyderabadi` does not match `hyderabad`, and did not before this change.

    Single tokens were already \\b-anchored, so this is pre-existing behaviour
    recorded rather than introduced — an adjective form is not a location string
    any board produces, and loosening the anchor to catch it would re-open every
    substring hole this module is built to avoid.
    """
    assert city_label("Hyderabadi Restaurant Group") == "other"
    assert city_label("Hyderabad") == "hyd"


def test_beng_is_not_a_marker():
    """Rejected deliberately: "Beng" is likelier to be Bengal than Bangalore."""
    assert city_label("Beng") != "blr"
    # West Bengal is a named Indian state containing none of the three cities,
    # so it is `other` — a confident "not here", not an unknown.
    assert city_label("West Bengal") == "other"
    # ...while the full name it was a substring of still works.
    assert city_label("Bengaluru") == "blr"


def test_bare_pun_is_not_a_marker():
    """Rejected deliberately: an English word, and PNQ is the real IATA code."""
    assert city_label("pun") != "pune"
    assert city_label("A pun about engineering") != "pune"
    assert city_label("Pune") == "pune"  # the name is untouched


# --- the promise that nothing already working changed ---------------------


@pytest.mark.parametrize(
    "location,expected",
    [
        ("India, Bengaluru", "blr"),
        ("Bengaluru-VTP, India", "blr"),
        ("IND-Bangalore-A, RMZ Infinity", "blr"),
        ("Electronic City, Bangalore", "blr"),
        ("India Hyderabad | 2 Locations", "hyd"),
        ("IND-Hyderabad-CapitaLand", "hyd"),
        ("India, Pune", "pune"),
        ("PUNE 05", "pune"),
        # A named Indian city that is not one of ours is `other`, the same as a
        # named foreign one: both are confident statements about where the job
        # is not. Only the strings that name no city stay unclear.
        ("IND-Mumbai-One World Center", "other"),
        ("Remote", "india-unclear"),
        ("", "india-unclear"),
        ("San Francisco, California, United States", "other"),
        ("London, UK", "other"),
        ("GBR London 5 Canada Square | 2 Locations", "other"),
        ("Colombo Sri Lanka | 2 Locations", "other"),
        ("Singapore", "other"),
    ],
)
def test_existing_labels_are_unchanged(location, expected):
    """Full names, India fallbacks, non-India anchors and vagueness: all as before."""
    assert city_label(location) == expected


def test_phrase_markers_still_match_when_anchored():
    """_hit anchoring phrases must not have broken the multi-word markers."""
    assert city_label("Electronics City, Karnataka") == "blr"
    assert city_label("HITEC City") == "hyd"
    assert city_label("New Delhi") == "other"
    assert city_label("Tamil Nadu") == "other"
    assert city_label("San Francisco") == "other"
    assert city_label("Multiple Locations") == "india-unclear"
