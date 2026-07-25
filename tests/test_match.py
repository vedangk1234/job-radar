"""The filter itself: which titles and cities survive, and which do not.

No database, no network, no fixtures — is_match is a pure function of two
values, so every case here is one line of setup and one assertion.

The bias under test is asymmetric on purpose. A keyword that fails to fire costs
one job in one digest. An exclusion that fires wrongly deletes a job from the
inbox permanently and silently, because nothing downstream ever reports what was
filtered out. So the substring-vs-word-boundary cases below are the important
ones: 'go' inside "Goa", 'qa' inside "Qatar", 'lead' inside "Already".
"""

from __future__ import annotations

import pytest

from notify.match import (
    Preferences,
    city_ok,
    compile_term,
    excluded,
    explain,
    is_match,
    keyword_ok,
    matched_terms,
    select_matches,
)

PREFS = Preferences()  # the seeded defaults, same values as db/schema.sql


def job(title: str, city: str = "blr", location: str = "Bengaluru, India") -> dict:
    return {
        "canonical_id": f"greenhouse:1:{title}",
        "title": title,
        "city": city,
        "location": location,
        "url": "https://example.test/job",
        "company": "Testco",
    }


# --- the headline cases from the spec -------------------------------------


def test_backend_role_in_bangalore_matches():
    """The job this whole pipeline exists to deliver."""
    assert is_match(job("Backend Engineer"), PREFS)


def test_senior_backend_engineer_is_excluded():
    """Passes city and keyword, and is still not sent. Exclusion wins outright."""
    j = job("Senior Backend Engineer")
    assert city_ok(j, PREFS) and keyword_ok(j, PREFS)
    assert excluded(j, PREFS)
    assert not is_match(j, PREFS)


def test_bangalore_sales_role_is_excluded():
    """Right city, wrong job. 'sales' is an exclusion and there is no keyword either."""
    j = job("Sales Development Representative")
    assert not keyword_ok(j, PREFS)
    assert excluded(j, PREFS)
    assert not is_match(j, PREFS)


def test_backend_role_in_another_city_is_dropped_on_city():
    """Stored, labelled 'other', and never emailed. Store loosely, notify tightly."""
    j = job("Backend Engineer", city="other", location="Singapore")
    assert keyword_ok(j, PREFS) and not excluded(j, PREFS)
    assert not city_ok(j, PREFS)
    assert not is_match(j, PREFS)


def test_india_unclear_backend_role_is_included():
    """'Remote, India' is a job worth seeing; ambiguity stays in the funnel."""
    j = job("Backend Engineer", city="india-unclear", location="Remote - India")
    assert is_match(j, PREFS)


# --- word boundaries ------------------------------------------------------


def test_goa_does_not_match_the_go_keyword():
    """The canonical substring failure. 'go ' must not fire inside "Goa"."""
    assert matched_terms("Goa Beach Operations", ["go "]) == []
    assert not keyword_ok(job("Operations Associate, Goa"), PREFS)


def test_a_short_term_still_matches_where_it_is_a_real_word():
    """…while the same pattern still catches the roles such a term is written for.

    Kept as an explicit term list rather than against PREFS: 'go ' is no longer
    in the resume-grounded keyword set, but it is still the clearest example of
    the boundary rule, and the rule is what this test is about.
    """
    assert matched_terms("Go Developer", ["go "]) == ["go "]
    assert matched_terms("Go/Python Engineer", ["go "]) == ["go "]


def test_devops_manager_is_excluded_on_manager():
    assert excluded(job("DevOps Manager"), PREFS)
    assert not is_match(job("DevOps Manager"), PREFS)


def test_already_does_not_match_the_lead_exclusion():
    """'lead' inside "Already" would silently delete jobs; \\b stops it."""
    assert matched_terms("Engineer, Already Onboarded Systems", ["lead"]) == []


def test_lead_matches_when_it_is_the_word():
    assert excluded(job("Lead Backend Engineer"), PREFS)
    assert excluded(job("Backend Engineering Lead"), PREFS)


@pytest.mark.parametrize(
    "title,term",
    [
        ("Rapid Prototyping Engineer", "api"),   # 'api' inside "Rapid"
        ("Capital Markets Engineer", "api"),     # 'api' inside "Capital"
        ("Qatar Regional Engineer", "qa"),       # 'qa' inside "Qatar"
        ("Overhead Systems Engineer", "head"),   # 'head' inside "Overhead"
        ("Django Backend Engineer", "go "),      # 'go' inside "Django"
        ("Internal Tools Engineer", "intern"),   # 'intern' inside "Internal"
    ],
)
def test_terms_never_fire_from_inside_a_longer_word(title, term):
    assert matched_terms(title, [term]) == []


@pytest.mark.parametrize(
    "title,term",
    [
        ("API Platform Engineer", "api"),
        ("QA Engineer", "qa"),
        ("Head of Engineering", "head"),
        ("Software Engineering Intern", "intern"),
    ],
)
def test_terms_do_fire_as_whole_words(title, term):
    assert matched_terms(title, [term]) == [term]


@pytest.mark.parametrize(
    "title", ["Back End Engineer", "Back-End Engineer", "Back_End Engineer", "back/end engineer"]
)
def test_multi_word_terms_span_the_separators_recruiters_actually_use(title):
    """One pattern covers space, hyphen, underscore and slash spellings."""
    assert matched_terms(title, ["back end"]) == ["back end"]


# --- terms carrying punctuation -------------------------------------------
#
# The stack names on a resume are not clean words. 'next.js' and 'node' both
# have to survive re.escape and still sit correctly against \b, and a dot in a
# term must be a literal dot rather than the regex "any character" — otherwise
# 'next.js' would quietly match "nextXjs" and, worse, the pattern could fail to
# compile at all.


@pytest.mark.parametrize(
    "title,term,expected",
    [
        ("Next.js Engineer", "next.js", True),
        ("Building with NEXT.JS", "next.js", True),
        ("NextJS Engineer", "nextjs", True),
        ("Nextxjs Engineer", "next.js", False),   # the dot is literal, not a wildcard
        ("Node Developer", "node", True),
        ("Node.js Backend Engineer", "node", True),
        ("Nodejs Engineer", "nodejs", True),
        ("Nodemon Maintainer", "node", False),
        ("REST API Engineer", "rest", True),
        ("Restaurant Operations Associate", "rest", False),
        ("ML Engineer", "ml engineer", True),
        ("ML-Engineer, Ranking", "ml engineer", True),
        ("HTML Developer", "ml engineer", False),
        ("React Native Developer", "react native", True),
    ],
)
def test_punctuated_and_short_stack_terms(title, term, expected):
    assert bool(matched_terms(title, [term])) is expected


def test_a_term_with_a_dot_compiles_and_escapes_it():
    assert compile_term("next.js").pattern == r"\bnext\.js\b"


@pytest.mark.parametrize(
    "noise",
    [
        "HTML Developer",
        "Restaurant Manager",
        "Capital Markets Associate",
        "Rapid Response Coordinator",
        "Goa Beach Operations",
    ],
)
def test_no_keyword_in_the_whole_set_fires_on_lookalike_noise(noise):
    """The set-wide version of the boundary rule: 42 terms, none of them greedy."""
    assert matched_terms(noise, PREFS.keywords) == []


def test_member_of_technical_staff_is_excluded_by_the_staff_exclusion():
    """A known collision between the two lists, asserted so it cannot surprise us.

    'member of technical staff' and 'mts' are keywords, and 'staff' is an
    exclusion — so an MTS posting matches a keyword and is then dropped anyway.
    Exclusions are meant to win, and this is what that costs. Changing it is a
    one-line edit to the preferences row, not to this code.
    """
    j = job("Member of Technical Staff")
    assert keyword_ok(j, PREFS)
    assert excluded(j, PREFS)
    assert not is_match(j, PREFS)

    assert is_match(job("MTS - Backend"), PREFS)  # the acronym form survives


def test_matching_is_case_insensitive():
    assert is_match(job("BACKEND ENGINEER"), PREFS)
    assert excluded(job("senior backend engineer"), PREFS)


# --- shape and edges ------------------------------------------------------


def test_a_title_with_no_keyword_is_not_a_match():
    j = job("Customer Success Associate")
    assert not keyword_ok(j, PREFS)
    assert not is_match(j, PREFS)


def test_missing_title_or_city_is_a_non_match_not_a_crash():
    assert not is_match({"title": None, "city": "blr"}, PREFS)
    assert not is_match({"title": "Backend Engineer", "city": None}, PREFS)
    assert not is_match({}, PREFS)


def test_preferences_are_data_and_changing_them_changes_the_answer():
    """The point of the preferences table: no redeploy, no code change."""
    j = job("Backend Engineer", city="other", location="Singapore")
    assert not is_match(j, PREFS)

    widened = Preferences(cities=("blr", "other"), keywords=("backend",), exclusions=())
    assert is_match(j, widened)


def test_preferences_from_row_accepts_lists_and_json_strings():
    """asyncpg hands back a list; the SQLite shim hands back JSON text."""
    from_list = Preferences.from_row(
        {"cities": ["blr"], "keywords": ["backend"], "exclusions": ["senior"]}
    )
    from_json = Preferences.from_row(
        {"cities": '["blr"]', "keywords": '["backend"]', "exclusions": '["senior"]'}
    )
    assert from_list == from_json == Preferences(("blr",), ("backend",), ("senior",))


def test_missing_preferences_row_falls_back_to_the_seeded_defaults():
    assert Preferences.from_row(None) == PREFS


def test_select_matches_keeps_input_order_and_drops_the_rest():
    jobs = [
        job("Backend Engineer"),
        job("Senior Backend Engineer"),
        job("Platform Engineer"),
        job("Backend Engineer", city="other", location="Berlin"),
        job("Recruiter"),
    ]
    kept = select_matches(jobs, PREFS)
    assert [j["title"] for j in kept] == ["Backend Engineer", "Platform Engineer"]


def test_explain_reports_the_first_failing_test():
    assert "not in preferences" in explain(job("Backend Engineer", city="other"), PREFS).reason
    assert "no keyword" in explain(job("Office Administrator"), PREFS).reason
    assert "'senior'" in explain(job("Senior Backend Engineer"), PREFS).reason
    assert "matched" in explain(job("Backend Engineer"), PREFS).reason
