"""The Lucknow leak: why `india-unclear` stopped meaning "somewhere in India".

A real InMobi posting — "SDE 2", `greenhouse:38:7956882`, location "Lucknow" —
reached the inbox as a match. Nothing about it was borderline: the title has no
excluded word and no level notation above 2, so it passed the title stage on the
strength of one keyword ("sde"), and the two city filters that should have
stopped it did not, for one reason each.

  1. `city_label("Lucknow")` returned `india-unclear`, because every India
     signal shared one bucket — the country, the states, and all forty-odd named
     cities. So "Lucknow" (a confident statement that the job is NOT in
     Bangalore, Hyderabad or Pune) was recorded as the same kind of fact as
     "Remote" (no statement at all).

  2. `india-unclear` was in preferences.cities, on the deliberate and defensible
     argument that a remote posting at an Indian company is worth one line of
     noise. That argument was about strings like "Remote". It was never about
     Lucknow, and the labeller made the two indistinguishable.

Either fix alone would have stopped this job. Both are here because they are
answers to different questions — what the label MEANS, and which labels the
filter ACCEPTS — and leaving either half in place would leave the other doing
work it was not designed for.

The sweep that followed found this was not one job: 14 of 61 matches were
`india-unclear`, and every one of them named a place. Noida (Adobe, Cadence),
Mohali (Zscaler), Lucknow (InMobi), and — labelled as Indian ambiguity on a
technicality — "Remote, United Arab Emirates" and "Remote - SF Bay Area".
"""

from __future__ import annotations

import pytest

from fetchers.normalize import city_label, india_funnel
from notify.match import DEFAULT_CITIES, Preferences, city_ok, is_match

PREFS = Preferences()


# --- the row that leaked ---------------------------------------------------


def test_the_inmobi_lucknow_row_is_excluded_on_city():
    """The verbatim row, as `notify/store.fetch_candidates` hands it over."""
    job = {
        "canonical_id": "greenhouse:38:7956882",
        "title": "SDE 2",
        "location": "Lucknow",
        "city": city_label("Lucknow"),
        "min_experience": None,
        "is_pm_role": False,
        "desc_checked": True,
        "desc_fetch_failed": False,
    }
    # The label was `other` when this regression was written and is
    # `other-india` since the 25-city rewrite split that bucket. What the test
    # is actually about is the line below it — the row does not reach an inbox —
    # so that is what it asserts now, and the label is checked for being outside
    # the filter rather than for being a particular string. A regression test
    # pinned to a spelling stops testing its regression the day the spelling
    # changes.
    assert job["city"] == "other-india"
    assert job["city"] not in PREFS.cities
    assert not city_ok(job, PREFS)
    assert not is_match(job, PREFS)


@pytest.mark.parametrize(
    "location",
    [
        "Lucknow",                      # InMobi, 2 rows
        "Lucknow, Uttar Pradesh",       # InMobi, the longer form on the same board
        "Noida",                        # Adobe, 3 rows
        "NOIDA",                        # Cadence, 4 rows
        "Noida | 2 Locations",          # Adobe, the Workday count form
        "Mohali, IND",                  # Zscaler, 2 rows
        "Remote - SF Bay Area",         # samsara — India-labelled on a technicality
        "Remote, United Arab Emirates",  # GitLab, likewise
    ],
)
def test_every_string_behind_the_14_leaked_matches_is_still_excluded(location):
    """Each of these was `india-unclear` and therefore a pass. None of them is.

    Asserted as "does not pass the gate" rather than as "== other", because the
    label these produce has now changed twice — `india-unclear` to `other` when
    the leak was fixed, `other` to `noida` / `other-india` / `non-india` when the
    vocabulary grew — while the thing being regression-tested never changed at
    all. Pinning the spelling would have made this test fail for the rewrite and
    stop protecting anything the moment somebody "fixed" it.
    """
    assert not city_ok({"city": city_label(location)}, PREFS)


# --- what the two labels mean now ------------------------------------------


@pytest.mark.parametrize(
    "location",
    ["Mumbai", "New Delhi", "Gurugram", "Chennai", "Kolkata", "Mysuru", "Jaipur",
     "Tamil Nadu", "West Bengal", "Uttar Pradesh", "Kerala"],
)
def test_a_named_indian_place_outside_the_three_cities_is_excluded(location):
    """A posting in Chennai is exactly as certainly not-Bangalore as one in
    London, and the label says so. The India-ness is not lost — see india_funnel
    below, and since the 25-city rewrite it is not even implicit: Chennai labels
    `chennai` and Jaipur labels `other-india`. What matters here is only that
    none of them is one of the operator's three cities.
    """
    assert not city_ok({"city": city_label(location)}, PREFS)


@pytest.mark.parametrize(
    "location",
    ["India", "Bharat", "Remote", "Remote India", "Multiple Locations", "",
     "Anywhere", "Hybrid", "APAC", "Karnataka", "Maharashtra", "Telangana"],
)
def test_a_string_that_names_no_city_is_still_unclear(location):
    """The inclusion bias survives, applied to the cases it was written for.

    The three states are here on purpose. Karnataka CONTAINS Bengaluru,
    Maharashtra contains Pune and Telangana contains Hyderabad, so a posting
    labelled only with one of them may well be in a city we want — which is
    genuine ambiguity.

    This test used to end "...unlike 'Uttar Pradesh', which cannot be." That is
    no longer true and the sentence is gone rather than reworded: UP contains
    Noida, which is a label somebody can now ask for, so UP is ambiguous in
    exactly the way Karnataka is. Six states moved across that line when the
    vocabulary grew; tests/test_city_vocabulary.py names all six. The rule did
    not change — "does this state contain a city somebody can ask for?" — only
    the list it is asked about.
    """
    assert city_label(location) == "india-unclear"


def test_precedence_is_unchanged_when_a_target_city_is_also_named():
    """The elsewhere test sits below the three cities, so a hit there still wins."""
    assert city_label("Mumbai / Bangalore") == "blr"
    assert city_label("Chennai, Hyderabad") == "hyd"
    assert city_label("Noida | Pune | 2 Locations") == "pune"
    assert city_label("Bangalore, Karnataka, India") == "blr"


# --- the gate --------------------------------------------------------------


def test_the_default_gate_is_exactly_three_cities():
    assert DEFAULT_CITIES == ("blr", "pune", "hyd")


@pytest.mark.parametrize("city", ["other", "india-unclear", None, "", "OTHER", "delhi"])
def test_nothing_but_a_configured_city_passes_the_gate(city):
    """Including NULL, including a label that does not exist, including case."""
    assert not city_ok({"city": city}, PREFS)


@pytest.mark.parametrize("city", ["blr", "pune", "hyd"])
def test_the_three_cities_still_pass(city):
    assert city_ok({"city": city}, PREFS)


# --- what the crawler still stores -----------------------------------------


@pytest.mark.parametrize(
    "location,funnel",
    [
        ("Bangalore", True),
        ("Lucknow", True),      # `other` to the FILTER, still India to the crawler
        ("Mumbai, India", True),
        ("Remote", True),       # vagueness stays in the funnel, as it always did
        ("", True),
        ("Singapore", False),
        ("San Francisco, California, United States", False),
        ("Remote - SF Bay Area", False),
    ],
)
def test_india_funnel_is_a_different_question_from_the_city_label(location, funnel):
    """fetchers/workday.py's unfiltered fallback asks "could this be India at all".

    It used to ask `city_label(...) != "other"`, which meant the same thing until
    named Indian cities started labelling `other`. Had the call site not moved
    with the labeller, an unscoped Workday tenant would have silently stopped
    STORING its Mumbai and Noida rows — a filtering decision, made in the crawl,
    against the one rule this project is built on.
    """
    assert india_funnel(location) is funnel
