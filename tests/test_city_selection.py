"""Phase 3, cities Slice B: the city gate reads what a person picked.

`preferences.cities` was the gate from Step 5 until this slice. It is now
`selected_cities` and `all_india`, with `cities` kept for one slice as the
fallback for a row that predates the backfill — see
notify.match.Preferences.effective_cities.

WHAT THIS FILE ASSERTS THAT tests/test_city_gate.py DOES NOT. That file is about
the LABELLER meeting the gate: which location strings end up outside the filter,
and the Lucknow regression that started it. This one is about the gate's own
shape — three branches, which column feeds it, and the two labels an explicit
selection may never match. They are separate because they fail for separate
reasons: a labeller change breaks the first, a rule change breaks this one.

THE FAIL-CLOSED POSTURE, STATED ONCE. Every ambiguity in this file resolves
toward NOT matching:

    no city answer at all       ->  match nothing
    india-unclear, picked       ->  match nothing (it belongs to all_india)
    non-india, any branch       ->  match nothing
    work_mode NULL              ->  not remote, so the city gate applies in full

That is the same direction city_ok has always leaned and the opposite of the
inclusion bias the DESCRIPTION tests carry. The asymmetry is deliberate and
notify/match.py argues it: a description can be absent, but a location string is
always present and always readable, so a label outside the filter is an answer
rather than a gap.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from notify.match import (
    CITY_SELECTION_NEVER_MATCHES,
    NON_INDIA_LABEL,
    Preferences,
    city_ok,
    is_match,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LABELS_TS = PROJECT_ROOT / "dashboard" / "src" / "lib" / "labels.ts"


def job(city: str | None, **extra):
    """A row shaped like the one notify/store.fetch_candidates hands over."""
    return {"title": "Backend Engineer", "city": city, **extra}


# --- which column feeds the gate -------------------------------------------


def test_selected_cities_is_the_gate():
    prefs = Preferences(selected_cities=("chennai",), cities=("blr",))
    assert city_ok(job("chennai"), prefs)
    assert not city_ok(job("blr"), prefs), "the legacy column must not still decide"


def test_an_empty_selection_falls_back_to_the_legacy_column():
    """The migration window: a row backfilled before Slice B existed.

    Its answer is in `cities` and nowhere else, and reading only the new column
    would silently empty that account's digest — a quiet week rather than an
    error, which is the failure this project spends its guards on.
    """
    prefs = Preferences(selected_cities=(), cities=("blr", "pune"))
    assert city_ok(job("blr"), prefs)
    assert city_ok(job("pune"), prefs)
    assert not city_ok(job("chennai"), prefs)


def test_the_fallback_cannot_rescue_a_blank_account():
    """Both columns empty is the brand-new account, and it matches nothing.

    The check that the fallback did not re-open the hole the blank seed opened:
    NEW_ACCOUNT_SEED writes `cities: []` and selected_cities takes its schema
    default of '{}', so there is nothing for the fallback to fall back TO. Same
    fail-closed answer as an unconfigured experience range, and for the same
    reason — a filter nobody chose, applied silently, is the expensive failure.
    """
    blank = Preferences(selected_cities=(), cities=(), all_india=False)
    for label in ("blr", "chennai", "mumbai", "other-india", "india-unclear", None):
        assert not city_ok(job(label), blank), label
    assert not is_match(job("blr"), blank)


# --- all over India ---------------------------------------------------------


@pytest.mark.parametrize(
    "label",
    ["blr", "hyd", "pune", "mumbai", "navi-mumbai", "noida", "gurugram", "chennai",
     "belagavi", "other-india", "india-unclear"],
)
def test_all_india_matches_every_indian_label(label):
    """Including both India-side buckets, which is the whole point of the switch.

    `other-india` is a named Indian place outside the 23 and `india-unclear` is a
    posting we could not pin down — a person who said "anywhere in India" has
    answered for both. Stated as a complement of non-india rather than as a list,
    so a 24th city cannot be forgotten out of it.
    """
    assert city_ok(job(label), Preferences(all_india=True, selected_cities=()))


def test_all_india_still_excludes_non_india():
    assert not city_ok(job(NON_INDIA_LABEL), Preferences(all_india=True, selected_cities=()))


def test_all_india_ignores_the_city_selection_entirely():
    """Exclusivity is in the RULE, not only in the UI that sets it.

    PUT /api/preferences refuses a body carrying both, so this state is not
    reachable through the app — which is exactly why the matcher is asserted
    directly. A row edited by hand must not produce a filter that is the
    intersection of the two answers, or anything else nobody asked for.
    """
    both = Preferences(all_india=True, selected_cities=("blr",))
    assert city_ok(job("chennai"), both), "the selection must not narrow all-India"
    assert city_ok(job("india-unclear"), both)
    assert not city_ok(job(NON_INDIA_LABEL), both)


def test_all_india_is_wider_than_any_selection():
    """The ordering property, which is what non-india's exclusion protects.

    Whatever an explicit selection matches, all-India matches too. A selection able
    to match a label all-India refuses would mean the narrow filter admitting rows
    the wide one does not — not a preference, an inversion.
    """
    wide = Preferences(all_india=True, selected_cities=())
    narrow = Preferences(selected_cities=("blr", "chennai", "other-india"))
    for label in ("blr", "chennai", "other-india", "india-unclear", NON_INDIA_LABEL, "mumbai"):
        if city_ok(job(label), narrow):
            assert city_ok(job(label), wide), label


# --- the two labels an explicit selection may never match -------------------


def test_the_excluded_set_is_exactly_these_two():
    """The membership, pinned — because the tests below PARAMETRIZE over it.

    WRITTEN AFTER A MUTATION CHECK FOUND THE HOLE. Deleting 'india-unclear' from
    the constant does not fail a test that iterates the constant: the case simply
    stops being generated, and a parametrized suite silently shrinks to cover the
    one member left. That is the shape of a guard that reports green about a rule
    it has stopped checking.

    So the set is asserted literally here, and the tests that iterate it are then
    honest about what they cover. Widening it fails too, which matters in the other
    direction: 'other-india' is a confident Indian location and adding it would
    quietly delete a legitimate answer from every account that had picked it.
    """
    assert CITY_SELECTION_NEVER_MATCHES == frozenset({"india-unclear", "non-india"})


@pytest.mark.parametrize("label", sorted(CITY_SELECTION_NEVER_MATCHES))
def test_naming_an_excluded_label_does_not_get_it_past_the_gate(label):
    """Checked on the LABEL, not filtered out of the list.

    The editor does not offer these and the route refuses them, so a row carrying
    one is already outside what the app can produce. This is the third layer, and
    the only one that holds for a row edited by hand in psql — which is what makes
    "it never matches" a property rather than a promise.
    """
    assert not city_ok(job(label), Preferences(selected_cities=("blr", label)))


def test_other_india_is_not_excluded():
    """The bucket that IS a real answer, and the reason the rule names two labels.

    `other-india` is a CONFIDENT statement that the posting is in India at a named
    place outside the 23 — Lucknow, Mohali, Patna. Ticking it means "anywhere else
    in India", which is coherent, and it carries none of the vagueness that
    disqualifies india-unclear. If this ever starts failing, the fail-closed rule
    has been widened past what it was argued for.
    """
    assert city_ok(job("other-india"), Preferences(selected_cities=("other-india",)))


def test_an_unlabelled_row_never_passes():
    """NULL city, in every branch. `or ""` is what makes it so.

    A row we never labelled is a bug rather than a doubt, and it must not read as a
    pass in the branch that is widest — which is all-India, where the only test is
    "not non-india" and the empty string trivially satisfies it. So this is
    asserted for the explicit branch, where it holds, and the all-India case is
    documented as the deliberate exception: a NULL city on an all-India filter DOES
    pass, because "anywhere in India" cannot be contradicted by a missing label.
    Zero live rows have a NULL city; lib/jobs.cityClause uses IS DISTINCT FROM so
    the SQL agrees with this either way.
    """
    assert not city_ok(job(None), Preferences(selected_cities=("blr",)))
    assert not city_ok(job(""), Preferences(selected_cities=("blr",)))
    assert city_ok(job(None), Preferences(all_india=True, selected_cities=()))


# --- the TypeScript mirror --------------------------------------------------
#
# The runtimes share no code (CLAUDE.md, "Two runtimes, one database"), so the
# city vocabulary exists twice and the umbrella groups exist once — in labels.ts,
# where the editor and the route both read them. tests/test_city_vocabulary.py
# already parses that file to keep CITY_LABELS honest against
# fetchers/normalize.py; these do the same for the two constants this slice added,
# because a group whose membership drifts is a checkbox that quietly stops
# selecting one of its cities.


def labels_source() -> str:
    return LABELS_TS.read_text(encoding="utf-8")


def group_members(heading: str) -> list[str]:
    """The `labels` array of one CITY_LABEL_GROUPS entry, by heading."""
    block = re.search(
        rf'heading:\s*"{re.escape(heading)}".*?labels:\s*\[(.*?)\]',
        labels_source(),
        re.DOTALL,
    )
    assert block, f"no CITY_LABEL_GROUPS entry with heading {heading!r} in labels.ts"
    return re.findall(r'"([^"]+)"', block.group(1))


@pytest.mark.parametrize(
    "heading,members",
    [
        ("Mumbai region", ["mumbai", "navi-mumbai", "mumbai-suburban", "thane"]),
        ("Delhi NCR", ["new-delhi", "gurugram", "noida"]),
    ],
)
def test_the_umbrella_groups_have_the_specced_membership(heading, members):
    """Tick-one-get-all expands to exactly these, and the set is asserted here.

    The expansion happens at SAVE time (api/preferences.expandUmbrellas), so what
    lands in selected_cities is the member list — which means this constant is the
    definition of what "All Mumbai areas" stores, and a label dropped from it is a
    city that silently stops being selected by the control that claims to select
    it. Sorted comparison: the editor renders in this order but the gate is a
    membership test, so order is presentation.
    """
    assert sorted(group_members(heading)) == sorted(members)


def test_every_umbrella_group_declares_an_umbrella():
    """A group with the members but no `umbrella` key renders no tick-all control.

    That failure is invisible on screen — the individual cities are all still
    there — so it is asserted rather than noticed.
    """
    for heading in ("Mumbai region", "Delhi NCR"):
        block = re.search(
            rf'heading:\s*"{re.escape(heading)}",\s*umbrella:\s*"[^"]+"',
            labels_source(),
        )
        assert block, f"the {heading!r} group has no umbrella label"


def test_the_two_excluded_labels_are_not_selectable_in_the_editor():
    """The first of the three layers: no control exists for them.

    CITY_LABEL_GROUPS is what the picker renders, so a label absent from every
    group cannot be ticked. Asserted against the constant rather than against the
    component, because the component maps over the constant and this is where the
    decision lives.
    """
    offered = re.findall(r'"([^"]+)"', labels_source().split("CITY_LABEL_GROUPS")[1].split("];")[0])
    for label in CITY_SELECTION_NEVER_MATCHES:
        assert label not in offered, (
            f"{label} is offered by the city picker. It can never match an explicit "
            f"selection (notify.match.CITY_SELECTION_NEVER_MATCHES), so a checkbox "
            f"for it is the silent-empty failure CITY_LABELS exists to prevent."
        )
