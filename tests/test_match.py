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
    detect_level,
    excluded,
    explain,
    is_match,
    keyword_ok,
    level_excluded,
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
    assert "level 4 above max_level 2" in explain(job("SDE-4"), PREFS).reason


# --- seniority LEVEL exclusion --------------------------------------------
#
# The word exclusions catch 'senior' and 'lead'. They cannot see "SDE III" or
# "Engineer 4", which say the same thing in a notation made of numbers — and
# numbers are also how job titles state YEARS OF EXPERIENCE. The two halves of
# this section are therefore not symmetric in cost:
#
#   a level that goes undetected  -> one extra line in one digest
#   an experience range read as a level -> the mid-level job is deleted, silently
#
# So the second parametrize below is the one that matters. Every case in it is a
# title a real board produced, and every one of them must survive.


ABOVE_MAX_LEVEL = [
    "Software Development Engineer III Data",   # roman numeral, mid-title
    "Software Development Engineer IV Android",
    "SDE 3",                                   # digit after a role token
    "SDE-4",                                   # hyphen, and NOT a range
    "SWE III",
    "Backend Engineer L4",                     # the L-form
    "Engineer (Level 5)",                      # spelled out, inside parentheses
    "Software Engineer VI",                    # 'vi' must not read as 'v'
    "Forward Deployed Engineer III",           # keyword hit, dropped anyway
]

WITHIN_MAX_LEVEL = [
    "Software Engineer II",                    # level 2 is wanted
    "SDE 2",
    "Forward Deployed Engineer - II",
    "Product Solution Engineer 2",
    "Network Delivery Engineer 1",             # level 1 is wanted
    "Ops Engineer 2 to 5 Years",               # "2 to 5" is a range, not a level
    "Backend Engineer, 3+ years experience",   # "3+ years" is experience
    "Python Developer (2-4 years)",            # both numbers are experience
    "Software Engineer",                       # no level stated at all
]


@pytest.mark.parametrize("title", ABOVE_MAX_LEVEL)
def test_titles_stating_level_three_or_above_are_excluded(title):
    assert level_excluded(job(title), PREFS)
    assert not is_match(job(title), PREFS)


@pytest.mark.parametrize("title", WITHIN_MAX_LEVEL)
def test_level_one_and_two_and_experience_ranges_are_never_level_excluded(title):
    """The expensive direction. A number here is a range or a junior level, never a drop."""
    assert not level_excluded(job(title), PREFS)


@pytest.mark.parametrize(
    "title,expected",
    [
        # the four recognised notations
        ("Software Development Engineer III Data", 3),
        ("Software Development Engineer IV Android", 4),
        ("Software Engineer VI", 6),
        ("Software Engineer II", 2),
        ("SDE 3", 3),
        ("SDE-4", 4),
        ("SDE 2", 2),
        ("SWE III", 3),
        ("Backend Engineer L4", 4),
        ("Engineer (Level 5)", 5),
        ("Forward Deployed Engineer - II", 2),
        ("Product Solution Engineer 2", 2),
        # level 1 is not mapped: nothing to exclude, so nothing to detect
        ("Network Delivery Engineer 1", None),
        ("Software Engineer", None),
        # the guard: every number here is experience, so no level is stated
        ("Ops Engineer 2 to 5 Years", None),
        ("Backend Engineer, 3+ years experience", None),
        ("Python Developer (2-4 years)", None),
        ("Software Engineer, React Native (3-5 Years)", None),
        ("Backend Engineer 4 yrs", None),
        ("Backend Engineer 3 y-o-e", None),
        ("Engineer 2 / 3 / 2", None),
        ("Engineer 3–5 years", None),           # en dash
    ],
)
def test_detect_level_reads_levels_and_refuses_experience(title, expected):
    assert detect_level(title) is expected


def test_the_highest_stated_level_wins():
    """"SDE II / III" is a III posting."""
    assert detect_level("SDE II / III") == 3
    assert level_excluded(job("SDE II / III"), PREFS)


def test_a_level_is_only_read_from_a_role_token_or_the_end_of_the_title():
    """A digit floating in the middle of a title is not a level."""
    assert detect_level("Engineer, Team 4 Payments") is None   # 'team' is not a level token
    assert detect_level("Backend Engineer 4") == 4             # trailing, and after a token


def test_max_level_is_data_and_raising_it_lets_the_seniors_back_in():
    """Same contract as cities and keywords: an UPDATE, not a deploy."""
    j = job("SDE 4")
    assert level_excluded(j, PREFS)
    assert not level_excluded(j, Preferences(max_level=4))
    assert is_match(j, Preferences(max_level=4))


def test_word_exclusions_and_level_exclusion_are_independent():
    """Either one alone is enough to drop a job; neither depends on the other."""
    word_only = job("Senior Backend Engineer")
    level_only = job("Backend Engineer L4")

    assert excluded(word_only, PREFS) and not level_excluded(word_only, PREFS)
    assert level_excluded(level_only, PREFS) and not excluded(level_only, PREFS)
    assert not is_match(word_only, PREFS) and not is_match(level_only, PREFS)


def test_missing_title_is_not_a_level_and_not_a_crash():
    assert detect_level(None) is None
    assert detect_level("") is None
    assert not level_excluded({}, PREFS)


# --- the forward-deployed-engineer keyword --------------------------------


def test_forward_deployed_engineer_is_a_keyword():
    """New to the set, so 'engineer' alone still does not qualify a title."""
    assert matched_terms("Forward Deployed Engineer", PREFS.keywords) == [
        "forward deployed engineer"
    ]
    assert "forward deployed engineer" in PREFS.keywords


def test_forward_deployed_engineer_level_two_is_a_match_and_level_three_is_not():
    """The keyword gets the title considered; the level decides whether it lands."""
    two = job("Forward Deployed Engineer - II")
    three = job("Forward Deployed Engineer III")

    assert keyword_ok(two, PREFS) and keyword_ok(three, PREFS)
    assert detect_level(two["title"]) == 2 and detect_level(three["title"]) == 3
    assert is_match(two, PREFS)          # keyword + level 2 + wanted city
    assert not is_match(three, PREFS)    # keyword, but level-excluded

    assert "level 3 above max_level 2" in explain(three, PREFS).reason


def test_senior_forward_deployed_engineer_still_loses_to_the_word_exclusion():
    """The new keyword does not buy an exemption from 'senior'."""
    assert not is_match(job("Senior Forward Deployed Engineer"), PREFS)


# --- the live rows this step was written for ------------------------------


def test_the_three_meesho_titles_drop_and_the_phonepe_razorpay_ones_remain():
    """The exact titles from the table, asserted so the step cannot silently regress.

    These are verbatim from the seeded jobs rows: the three Meesho SDE III/IV
    postings are what motivated max_level, and the PhonePe/Razorpay matches are
    what must not become collateral damage — including the React Native role,
    whose "(3-5 Years)" is precisely the experience range the guard exists for.
    """
    dropped = [
        "Software Development Engineer III Data",
        "Software Development Engineer III DevOps",
        "Software Development Engineer IV Android",
    ]
    for title in dropped:
        j = job(title)
        assert keyword_ok(j, PREFS), title       # it was a candidate...
        assert level_excluded(j, PREFS), title   # ...and the level is why it went
        assert not is_match(j, PREFS), title

    kept = [
        "Software Engineer, React Native (3-5 Years)",
        "Backend Engineer",
        "Forward Deployed Engineer - II",
    ]
    for title in kept:
        assert is_match(job(title), PREFS), title
