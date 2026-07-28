"""Misspelled city names, and the near-misses that must NOT be treated as one.

PhonePe's Greenhouse board writes "Banglore" on every posting from that office —
no second 'a'. Three real Bangalore jobs sat in `other`, the one label the
default preferences drop, because of a missing letter.

The marker was chosen from the data rather than from imagination:
scripts/survey_locations.py lists every distinct location string in the dropped
buckets, and a fuzzy pass compared every token in all 1197 stored rows against
the five real city names. "banglore" was the only near-miss in the table. That is
why this file adds one spelling and asserts the absence of the others — a marker
nothing in the database needs is false-positive surface bought with no recall,
which is exactly the test "beng" and bare "pun" failed in test_city_codes.py.

Half of this file is therefore decoys, and the most important one is real: LSEG
posts from "THA Bangkok One Bangkok". "Bangkok" and "Banglore" share four
letters, and any rule reaching for a "bang" prefix would relabel three Thai jobs
as Bangalore. \\b-anchoring a whole non-word token is what makes that impossible.
"""

from __future__ import annotations

import pytest

from fetchers.normalize import city_label


# --- the strings this change exists for -----------------------------------


@pytest.mark.parametrize(
    "location",
    [
        "Banglore",          # verbatim: the 3 PhonePe rows
        "banglore",
        "BANGLORE",
        "Banglore, India",
        "Banglore, Karnataka",
        "Banglore/Hyderabad",   # precedence unchanged: blr is checked first
        "India - Banglore",
        "IND-Banglore-Office",  # norm() turns the hyphens into spaces first
    ],
)
def test_the_phonepe_misspelling_now_labels_blr(location):
    assert city_label(location) == "blr"


# --- the decoys -----------------------------------------------------------


@pytest.mark.parametrize(
    "location",
    [
        # The one that actually exists in this database, 3 LSEG rows. Any rule
        # matching a "bang" prefix instead of the whole token relabels these.
        "THA Bangkok One Bangkok | 2 Locations",
        "Bangkok",
        "Bangkok, Thailand",
        "Bangor, Maine",
        "Bangor, Wales",
        "Bangui, Central African Republic",
        "Bangladesh",
        "Dhaka, Bangladesh",
    ],
)
def test_places_that_merely_start_like_bangalore_are_not_blr(location):
    assert city_label(location) != "blr"


@pytest.mark.parametrize(
    "location",
    ["banglorex", "xbanglore", "abanglorea", "Bangloreans"],
)
def test_the_misspelling_never_fires_inside_a_longer_word(location):
    assert city_label(location) != "blr"


def test_west_bengal_is_still_a_state_and_not_a_city():
    """The fuzzy pass's only other near-miss. It is a real place, correctly not ours."""
    assert city_label("Kolkata, West Bengal") == "other"
    assert city_label("West Bengal, India") == "other"


# --- what was deliberately left out ---------------------------------------


def test_the_district_forms_need_no_marker_of_their_own():
    """"Bangalore Urban" already matches — via the token "bangalore" inside it.

    _hit anchors on word boundaries, not on the whole string, so the existing
    full names cover the district spellings. Recorded as a test rather than
    added as a marker, so the redundancy stays visible to whoever reads the
    marker list next.
    """
    assert city_label("Bangalore Urban") == "blr"
    assert city_label("Bengaluru Urban, Karnataka") == "blr"
    assert city_label("Bangalore Urban District") == "blr"
    # ...and the one spelling the brief asked about that WAS already present.
    assert city_label("Bangaluru") == "blr"


@pytest.mark.parametrize(
    "location", ["Hyderbad", "Hydrabad", "Puna", "Poona", "Bengalore", "Banaglore"]
)
def test_unevidenced_misspellings_are_not_markers(location):
    """No posting in this database is spelled any of these ways.

    Asserting the CURRENT behaviour, not endorsing it. Each of these is a
    plausible typo, and every one of them is absent from all 1197 stored rows
    and from tests/data — so none earned a marker. If a board starts writing one,
    scripts/survey_locations.py surfaces it and this test is the place the
    decision gets revisited.
    """
    assert city_label(location) not in ("blr", "hyd", "pune")


# --- the promise that nothing already working changed ---------------------


@pytest.mark.parametrize(
    "location,expected",
    [
        # Correct spellings, from real rows in the database.
        ("Bangalore", "blr"),
        ("BANGALORE", "blr"),
        ("India, Bengaluru", "blr"),
        ("Bangalore, Karnataka", "blr"),
        ("Bangalore - EC", "blr"),
        ("Mumbai / Bangalore", "blr"),
        ("IND-BLR-Divyasree Technopolis", "blr"),
        ("Hyderabad", "hyd"),
        ("India Hyderabad | 2 Locations", "hyd"),
        ("IND-Hyderabad-CapitaLand", "hyd"),
        ("PUNE 05", "pune"),
        ("India, Pune", "pune"),
        # A named Indian city outside the three is `other` — it says where the
        # job is, and the answer is "not here". Only strings naming no city at
        # all stay `india-unclear`.
        ("Noida", "other"),
        ("India - Mumbai", "other"),
        ("IND-Mumbai-One World Center", "other"),
        ("Mysore", "other"),
        ("Remote India", "india-unclear"),
        ("Hubballi", "other"),
        ("Nashik", "other"),
        ("Varanasi", "other"),
        ("San Francisco, California, United States", "other"),
        ("Colombo Sri Lanka | 2 Locations", "other"),
        ("PHL Taguig City CitiPlaza | 2 Locations", "other"),
        ("Malaysia", "other"),
    ],
)
def test_existing_labels_are_unchanged(location, expected):
    assert city_label(location) == expected
