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
    MAX_MIN_EXPERIENCE,
    Preferences,
    city_ok,
    compile_term,
    detect_level,
    excluded,
    experience_excluded,
    explain,
    is_match,
    keyword_ok,
    level_excluded,
    matched_terms,
    pm_excluded,
    select_matches,
    title_stage_ok,
)

PREFS = Preferences()  # the seeded defaults, same values as db/schema.sql


def job(
    title: str,
    city: str = "blr",
    location: str = "Bengaluru, India",
    *,
    min_experience: int | None = None,
    is_pm_role: bool = False,
) -> dict:
    """A candidate row. The description-derived columns default to "not looked".

    NULL / FALSE is what a freshly crawled row carries, so every test that says
    nothing about them is testing the ordinary, unenriched case — which must keep
    behaving exactly as it did before the second stage existed.
    """
    return {
        "canonical_id": f"greenhouse:1:{title}",
        "title": title,
        "city": city,
        "location": location,
        "url": "https://example.test/job",
        "company": "Testco",
        "min_experience": min_experience,
        "is_pm_role": is_pm_role,
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


# --- the description stage ------------------------------------------------
#
# Two more AND clauses, reading two columns that notify/description.py computed
# from text this module never sees. The evidence is gone; the conclusion is a
# integer and a boolean on the row, read here exactly as `city` is read.
#
# The asymmetry is the same as everywhere else in this file, and here it is
# spelled into the NULL handling: min_experience IS NULL covers "never read",
# "read and stated nothing" and "the fetch failed", and all three keep the job.
# Only a number we are confident about, above the ceiling, drops one.


def test_a_role_demanding_three_years_is_dropped():
    """The headline case. 3 > 2, so this stops arriving."""
    j = job("Full Stack Builder", min_experience=3)
    assert title_stage_ok(j, PREFS)          # it passed every title test...
    assert experience_excluded(j)            # ...and the description is why it went
    assert not is_match(j, PREFS)


def test_a_role_demanding_two_years_is_kept():
    """The ceiling is inclusive. Two years is the job we are looking for."""
    j = job("Backend Engineer", min_experience=2)
    assert not experience_excluded(j)
    assert is_match(j, PREFS)


def test_an_unknown_minimum_is_kept():
    """NULL is silence, and silence is never a reason to delete a job."""
    j = job("Backend Engineer", min_experience=None)
    assert not experience_excluded(j)
    assert is_match(j, PREFS)


def test_a_pm_role_is_dropped_however_the_title_reads():
    """"Full Stack Builder" says nothing. The description said it was a PM job."""
    j = job("Full Stack Builder", is_pm_role=True)
    assert title_stage_ok(j, PREFS)
    assert pm_excluded(j)
    assert not is_match(j, PREFS)


@pytest.mark.parametrize(
    "min_experience,is_pm_role,expected",
    [
        (None, False, True),    # nothing known -> kept
        (0, False, True),       # explicitly entry level
        (1, False, True),
        (2, False, True),       # the ceiling itself
        (3, False, False),      # one year over
        (8, False, False),
        (None, True, False),    # PM alone is enough
        (2, True, False),       # ...even at a welcome experience level
        (3, True, False),       # the Razorpay case: both clauses fire
    ],
)
def test_the_two_description_clauses_across_the_whole_range(
    min_experience, is_pm_role, expected
):
    j = job("Backend Engineer", min_experience=min_experience, is_pm_role=is_pm_role)
    assert is_match(j, PREFS) is expected


def test_the_description_stage_cannot_rescue_a_title_stage_failure():
    """The clauses only ever subtract. AND, not OR."""
    senior = job("Senior Backend Engineer", min_experience=1)
    berlin = job("Backend Engineer", city="other", location="Berlin", min_experience=0)

    assert not is_match(senior, PREFS)
    assert not is_match(berlin, PREFS)


def test_title_stage_ok_is_exactly_the_old_four_test_filter():
    """The seam the enrichment pass fetches against, unchanged by its arrival."""
    assert title_stage_ok(job("Backend Engineer"), PREFS)
    assert not title_stage_ok(job("Senior Backend Engineer"), PREFS)
    assert not title_stage_ok(job("SDE-4"), PREFS)
    assert not title_stage_ok(job("Backend Engineer", city="other"), PREFS)
    assert not title_stage_ok(job("Customer Success Associate"), PREFS)

    # ...and it is deliberately blind to the description columns, because those
    # are what it exists to decide are worth fetching.
    assert title_stage_ok(job("Backend Engineer", min_experience=9, is_pm_role=True), PREFS)


def test_a_row_that_predates_the_columns_behaves_as_unknown():
    """A dict with neither key is the old shape, and the old answer."""
    old_row = {"title": "Backend Engineer", "city": "blr"}
    assert not pm_excluded(old_row)
    assert not experience_excluded(old_row)
    assert is_match(old_row, PREFS)


def test_an_unreadable_min_experience_is_treated_as_unknown():
    """A garbage value keeps the job rather than raising mid-digest."""
    assert not experience_excluded({"min_experience": "three"})
    assert not experience_excluded({"min_experience": ""})


def test_explain_names_the_description_clause_that_dropped_the_job():
    assert "PM/management" in explain(job("Full Stack Builder", is_pm_role=True), PREFS).reason
    assert f"above ceiling {MAX_MIN_EXPERIENCE}" in explain(
        job("Full Stack Builder", min_experience=3), PREFS
    ).reason
    assert "matched" in explain(job("Backend Engineer", min_experience=2), PREFS).reason


def test_pm_wins_over_experience_in_the_explanation_when_both_fire():
    """Both are true of the Razorpay posting; the report names one, consistently."""
    report = explain(job("Full Stack Builder", min_experience=3, is_pm_role=True), PREFS)
    assert not report.matched
    assert report.min_experience == 3
    assert report.is_pm_role is True
    assert "PM/management" in report.reason


def test_the_two_razorpay_full_stack_builders_now_drop():
    """The live rows this step was written for.

    Both were kept by the title filter — "Full Stack Builder" hits the 'full
    stack' keyword, states no level, sits in Bengaluru — and both are a technical
    PM role asking for 3-6 years, which only the description says.
    """
    razorpay = job("Full Stack Builder", min_experience=3, is_pm_role=True)
    assert title_stage_ok(razorpay, PREFS)
    assert not is_match(razorpay, PREFS)


def test_select_matches_applies_both_new_clauses():
    jobs = [
        job("Backend Engineer"),
        job("Full Stack Builder", min_experience=3, is_pm_role=True),
        job("Platform Engineer", min_experience=2),
        job("Python Developer", min_experience=5),
        job("React Native Engineer", is_pm_role=True),
    ]
    kept = select_matches(jobs, PREFS)
    assert [j["title"] for j in kept] == ["Backend Engineer", "Platform Engineer"]
