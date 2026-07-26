r"""The second-stage filter: what a description says, and what it does not say.

No network anywhere in this file. The Razorpay "Full Stack Builder" description
is checked in as tests/data/razorpay_full_stack_builder.txt — the real thing,
produced by running the real fetch_description against the real Greenhouse
single-job endpoint and saving what came back — so every assertion below is
about text a live board actually served, not about a sentence chosen to be easy
to parse.

That distinction is the point of the file. The Razorpay text is the adversarial
case for BOTH detectors at once:

    "$180+ billion in annualized transactions"     a number
    "a 3000+ strong team"                          a number
    "Founded in 2014"                              a number
    "raised over $740 million to date"             a number
    "over 100 of India's unicorns"                 a number
    "3-6 years building software as technical PM"   the requirement
    "Most engineers, PMs & designers use AI"       a mention, not a role
    "In 3–4 lines, describe … as a Product Manager" the role, in the form

A detector that reads bare numbers finds five requirements in that text. A
detector that reads bare job titles finds a PM role in every backend JD ever
written. Both failures are silent, and both delete real jobs from the inbox.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from notify.description import (
    description_from_row,
    detect_min_experience,
    detect_pm_role,
    enrich_candidates,
    html_to_text,
    native_id_from,
)

DATA_DIR = Path(__file__).resolve().parent / "data"

RAZORPAY = (DATA_DIR / "razorpay_full_stack_builder.txt").read_text(encoding="utf-8")

# A perfectly ordinary backend JD. Mentions product managers, because every
# engineering JD does, and states an experience range this filter wants to keep.
BACKEND_JD = """
About the role
We are looking for a Backend Engineer to join our Payments team in Bengaluru.
You will design and ship services that move money, own them in production, and
partner with product managers and designers to decide what to build next.

What you will need
- 2-4 years of experience building backend services in Python, Go or Java
- Comfort with PostgreSQL, Kafka and Kubernetes
- A bias for shipping

About us
Founded in 2014, we process $180 billion in annualized transactions for over
100 of India's unicorns, and have raised over $740 million to date. You will be
part of a 3000+ strong team.
"""

# The same shape with no experience line at all, which is the common case.
NO_EXPERIENCE_JD = """
Software Engineer, Platform
You will build and operate the internal platform every other team deploys on.
Strong fundamentals in distributed systems. You will work closely with product
managers, designers and the engineering manager who owns the roadmap.
"""


# --- detect_min_experience: the requirement -------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("3-6 years building software", 3),          # range, lower bound is the minimum
        ("2-4 years of experience", 2),
        ("0-3 years", 0),                            # an entry-level opening states itself
        ("5+ years experience", 5),
        ("minimum 3 years", 3),
        ("at least 2 years", 2),
        ("fresher", 0),
        (NO_EXPERIENCE_JD, None),                    # a JD with no experience line
    ],
)
def test_detect_min_experience_reads_the_stated_minimum(text, expected):
    assert detect_min_experience(text) is expected


# The six strings from the Razorpay description that a number-reading detector
# would mistake for a requirement. Every one must be invisible here.
DECOYS = [
    "$180+ billion",
    "3000+ strong team",
    "Founded in 2014",
    "raised over $740 million",
    "100 of India's unicorns",
    "processes $180 billion in annualized transactions",
]


@pytest.mark.parametrize("decoy", DECOYS)
def test_money_headcounts_and_dates_are_never_read_as_experience(decoy):
    """The expensive direction. Each of these is a real line from a real board."""
    assert detect_min_experience(decoy) is None


def test_the_decoys_are_invisible_even_all_together_in_one_document():
    """Individually harmless is not the same as harmless in combination."""
    assert detect_min_experience("\n".join(DECOYS)) is None


def test_the_razorpay_description_reads_as_a_three_year_minimum():
    """One requirement in 5 KB of prose, five decoys, and the right answer.

    "3-6 years building software as technical PM" is the only experience-anchored
    figure in the whole document. The minimum is 3, which is above the ceiling,
    which is why this posting stops arriving.
    """
    assert detect_min_experience(RAZORPAY) == 3


@pytest.mark.parametrize(
    "line,expected",
    [
        # The one that got away. A 40-character context window cut "experience"
        # in half and read this five-to-nine-year role as stating no requirement.
        ("5–9 years of full-time iOS app development experience", 5),
        ("Over 1-2 years of full time Android App development experience", 1),
        ("Mandate 3-5 years of full time experience", 3),
        ("Software Engineer 4-6yrs - SRE Tools", None),   # no anchor on the line
        ("4+ years experience in Software Development", 4),
    ],
)
def test_the_real_phonepe_requirement_lines(line, expected):
    """Verbatim lines from four live PhonePe descriptions.

    They are here because the first version of this detector agreed with three of
    them and silently missed the first — which is the whole failure mode this
    filter exists to prevent, one level down.
    """
    assert detect_min_experience(line) is expected


def test_a_range_may_be_anchored_anywhere_on_its_own_line_but_not_across_one():
    """`N-M years` is a shape prose does not produce by accident, so it gets room.

    A newline still stops it, because a benefits paragraph must not lend the word
    "experience" to a duration in the bullet above it.
    """
    assert detect_min_experience("2-4 years designing and shipping backend experience") == 2
    assert detect_min_experience("2-4 years\nexperience is valued here") is None


def test_a_single_duration_in_ordinary_prose_is_still_not_a_requirement():
    """The reason `N years` keeps the tight rule while `N-M years` does not."""
    assert detect_min_experience(
        "In the 3 years since we were founded we have built deep experience in payments."
    ) is None


def test_the_smallest_confident_minimum_wins():
    """A JD that asks for 2 and prefers 5 is a 2-year job."""
    assert detect_min_experience("2+ years of experience required, 5+ years preferred") == 2


def test_a_number_without_a_duration_unit_is_not_experience():
    """The unit is the first of the two anchors, and it is not optional."""
    assert detect_min_experience("3 of experience") is None
    assert detect_min_experience("team of 8 engineers with deep experience") is None


def test_a_duration_without_experience_language_is_not_experience():
    """And the context word is the second anchor, equally not optional."""
    assert detect_min_experience("this role runs on a 2 week sprint cadence") is None
    assert detect_min_experience("the programme lasts 6 years") is None


def test_entry_level_language_reads_as_zero():
    assert detect_min_experience("This is an entry-level opening for new grads.") == 0
    assert detect_min_experience("Freshers welcome") == 0


def test_missing_text_is_not_a_requirement_and_not_a_crash():
    assert detect_min_experience(None) is None
    assert detect_min_experience("") is None
    assert detect_min_experience("   ") is None


# --- detect_pm_role: the role, not the colleague --------------------------


def test_the_razorpay_full_stack_builder_is_a_pm_role():
    """The case this detector exists for.

    Nothing in the title says it. The description says it twice: the requirement
    is "3-6 years building software as technical PM", and the application form
    asks the candidate to describe their work "as a Product Manager". Either one
    alone is the role telling you what it is.
    """
    assert detect_pm_role("Full Stack Builder", RAZORPAY) is True


def test_a_plain_backend_jd_is_not_a_pm_role():
    assert detect_pm_role("Backend Engineer", BACKEND_JD) is False


def test_collaborating_with_product_managers_is_not_a_pm_role():
    """The false positive that would delete most of the digest.

    Almost every engineering JD names a product manager, because almost every
    engineer works with one. The phrase has to be about the role itself.
    """
    text = "You will collaborate with product managers and designers to scope work."
    assert detect_pm_role("Backend Engineer", text) is False


@pytest.mark.parametrize(
    "text",
    [
        "You will partner with product management on the roadmap.",
        "You will report to the engineering manager for the Payments group.",
        "Work alongside our product owner during sprint planning.",
        "Most engineers, PMs & designers use AI. Few are AI-native.",
    ],
)
def test_a_management_term_about_someone_else_is_never_the_role(text):
    assert detect_pm_role("Software Engineer", text) is False


@pytest.mark.parametrize(
    "text",
    [
        "3-6 years building software as technical PM",
        "describe how you leverage AI in your work as a Product Manager",
        "We are hiring a program manager for the Platform org.",
        "You will be the product owner for the checkout surface.",
        "In this role you will manage a team of six engineers.",
        "This position has four direct reports.",
    ],
)
def test_the_role_framed_as_management_is_a_pm_role(text):
    assert detect_pm_role("Builder", text) is True


@pytest.mark.parametrize(
    "title",
    ["Product Manager", "Technical Program Manager", "Engineering Manager, Platform"],
)
def test_a_title_that_names_the_role_needs_no_framing(title):
    """A posting called "Engineering Manager" is not collaborating with one."""
    assert detect_pm_role(title, "") is True


def test_missing_text_and_title_are_not_a_pm_role_and_not_a_crash():
    assert detect_pm_role(None, None) is False
    assert detect_pm_role("", "") is False


# --- HTML -> text ---------------------------------------------------------


def test_greenhouse_escaped_markup_is_unescaped_before_it_is_stripped():
    """Greenhouse ships `content` HTML-escaped: not one real angle bracket in it.

    Without the pre-unescape the whole body survives as visible tag soup, and
    "&lt;li&gt;3-6 years" never separates from the line above it.
    """
    raw = "&lt;p&gt;3-6 years of experience&lt;/p&gt;&lt;p&gt;Bengaluru&lt;/p&gt;"
    assert html_to_text(raw) == "3-6 years of experience\nBengaluru"


def test_real_html_with_literal_escaped_brackets_is_not_mangled():
    """A description that contains BOTH tags and a typed `&lt;` keeps the latter."""
    assert html_to_text("<p>Use Array&lt;String&gt; generics</p>") == "Use Array<String> generics"


def test_block_tags_become_line_breaks_so_list_items_stay_apart():
    """Fusing two bullets would invent sentences neither of them said."""
    text = html_to_text("<ul><li>2 years</li><li>experience with Kafka</li></ul>")
    assert text == "2 years\nexperience with Kafka"
    # ... and the fused version would have read as a requirement it never stated.
    assert detect_min_experience(text) is None


def test_scripts_and_styles_are_dropped_entirely():
    assert html_to_text("<style>p{color:red}</style><p>Backend Engineer</p>") == (
        "Backend Engineer"
    )


def test_empty_and_non_string_markup_is_none():
    assert html_to_text(None) is None
    assert html_to_text("") is None
    assert html_to_text("   ") is None
    assert html_to_text("<div>  </div>") is None


# --- per-ATS row reading --------------------------------------------------


def test_lever_requirements_come_from_lists_not_just_the_intro():
    """The bug this test exists to prevent.

    Lever keeps the intro in `description` and the REQUIREMENTS in `lists` — so a
    reader that took only the description would find no experience line in any
    Lever posting, ever, and every one of them would look like an open door.
    """
    row = {
        "descriptionPlain": "About the Team\nWe build the Voice AI platform.",
        "lists": [
            {
                "text": "What you will need",
                "content": "<li><p>2-5 years of experience in software engineering</p></li>",
            }
        ],
        "additionalPlain": "About us\nWelcome to Meesho.",
    }
    text = description_from_row("lever", row)
    assert "About the Team" in text
    assert "What you will need" in text
    assert "About us" in text
    assert detect_min_experience(text) == 2


def test_lever_falls_back_to_the_html_fields_when_the_plain_ones_are_absent():
    row = {"description": "<p>3+ years of experience</p>", "lists": []}
    assert description_from_row("lever", row) == "3+ years of experience"


def test_ashby_prefers_the_plain_description_it_already_publishes():
    row = {"descriptionPlain": "5+ years experience", "descriptionHtml": "<p>ignored</p>"}
    assert description_from_row("ashby", row) == "5+ years experience"


def test_ashby_falls_back_to_html_when_plain_is_empty():
    row = {"descriptionPlain": "  ", "descriptionHtml": "<p>2 years of experience</p>"}
    assert description_from_row("ashby", row) == "2 years of experience"


def test_workable_and_recruitee_read_requirements_as_well_as_description():
    """Same trap as Lever: the years line lives in the requirements field."""
    workable = {
        "description": "<p>Join us.</p>",
        "requirements": "<p>minimum 3 years</p>",
        "benefits": "<p>Health cover</p>",
    }
    assert detect_min_experience(description_from_row("workable", workable)) == 3

    recruitee = {"description": "<p>Join us.</p>", "requirements": "<p>at least 2 years</p>"}
    assert detect_min_experience(description_from_row("recruitee", recruitee)) == 2


def test_an_ats_with_no_verified_description_route_returns_none():
    """Unknown is a real answer, and it means keep the job."""
    assert description_from_row("smartrecruiters", {"name": "Engineer"}) is None
    assert description_from_row("workday", {}) is None


# --- canonical_id -> the vendor's own id ----------------------------------


@pytest.mark.parametrize(
    "canonical_id,expected",
    [
        ("greenhouse:9:4712960005", "4712960005"),
        ("lever:3:fcceab83-4d26-4ea3-a5bc-b137b7e5ee5a", "fcceab83-4d26-4ea3-a5bc-b137b7e5ee5a"),
        ("hash:6f1c2d3e4f5a6b7c8d9e0f1a", None),   # the board published no id
        ("greenhouse:9:", None),
        ("nonsense", None),
        (None, None),
    ],
)
def test_native_id_is_recovered_from_the_canonical_id(canonical_id, expected):
    """`{ats}:{company_id}:{native_id}` — so no extra column is needed to ask a vendor."""
    assert native_id_from(canonical_id) == expected


# --- the enrichment pass --------------------------------------------------


async def stub_fetcher(texts: dict[str, str | None]):
    """A fetch_description that answers from a dict keyed by canonical_id."""

    async def fetch(company, job):
        return texts.get(job["canonical_id"])

    return fetch


async def test_enrichment_stores_three_facts_and_no_text(digest_conn):
    """The whole discipline, asserted against the actual table.

    Every column of the jobs table is listed, and none of them holds a
    description. If a future change adds one, this test is what notices.
    """
    await digest_conn.add_job("razorpay-1", title="Full Stack Builder")
    await digest_conn.add_job("meesho-1", title="Forward Deployed Engineer - II")

    from notify.store import fetch_candidates

    candidates = await fetch_candidates(digest_conn)
    fetch = await stub_fetcher(
        {
            "razorpay-1": RAZORPAY,
            "meesho-1": "2-5 years of experience in software engineering",
        }
    )
    report = await enrich_candidates(digest_conn, candidates, fetcher=fetch)

    rows = await digest_conn.snapshot()
    assert rows["razorpay-1"]["min_experience"] == 3
    assert rows["razorpay-1"]["is_pm_role"] == 1
    assert rows["razorpay-1"]["desc_checked"] == 1
    assert rows["meesho-1"]["min_experience"] == 2
    assert rows["meesho-1"]["is_pm_role"] == 0

    # No column anywhere holds the text these facts came from.
    for row in rows.values():
        for column, value in row.items():
            assert "3-6 years" not in str(value), column
            assert "Razorpay is one of" not in str(value), column

    assert {e["canonical_id"] for e in report} == {"razorpay-1", "meesho-1"}
    assert all(e["fetched"] for e in report)


async def test_a_failed_fetch_marks_the_job_checked_and_keeps_it(digest_conn):
    """None means "we do not know", which means keep — and means do not re-ask.

    Re-fetching a dead endpoint every thirty minutes for as long as the posting
    stays open is not persistence, so desc_checked is set either way.
    """
    await digest_conn.add_job("dead-board", title="Backend Engineer")

    from notify.store import fetch_candidates

    candidates = await fetch_candidates(digest_conn)
    fetch = await stub_fetcher({})  # answers None for everything
    report = await enrich_candidates(digest_conn, candidates, fetcher=fetch)

    row = (await digest_conn.snapshot())["dead-board"]
    assert row["min_experience"] is None
    assert row["is_pm_role"] == 0
    assert row["desc_checked"] == 1
    assert report[0]["fetched"] is False


async def test_an_already_checked_job_is_never_fetched_again(digest_conn):
    """desc_checked is the gate, and it is the only thing making this cheap."""
    await digest_conn.add_job(
        "seen-before", title="Backend Engineer", min_experience=2, desc_checked=True
    )

    from notify.store import fetch_candidates

    calls: list[str] = []

    async def counting_fetch(company, job):
        calls.append(job["canonical_id"])
        return "5+ years experience"

    report = await enrich_candidates(
        digest_conn, await fetch_candidates(digest_conn), fetcher=counting_fetch
    )

    assert calls == []
    assert report == []
    assert (await digest_conn.snapshot())["seen-before"]["min_experience"] == 2  # unchanged


async def test_enrichment_updates_the_rows_in_place_for_the_caller(digest_conn):
    """So the very next is_match() sees the facts without a second SELECT."""
    await digest_conn.add_job("pm-role", title="Full Stack Builder")

    from notify.match import Preferences, is_match
    from notify.store import fetch_candidates

    candidates = await fetch_candidates(digest_conn)
    assert is_match(candidates[0], Preferences())  # before: nothing known, so kept

    await enrich_candidates(
        digest_conn, candidates, fetcher=await stub_fetcher({"pm-role": RAZORPAY})
    )

    assert candidates[0]["min_experience"] == 3
    assert candidates[0]["is_pm_role"] is True
    assert not is_match(candidates[0], Preferences())  # after: dropped, twice over


async def test_enrichment_never_touches_the_notified_flag(digest_conn):
    """It is not a lifecycle write. Nothing here can make a job look sent."""
    await digest_conn.add_job("a", title="Backend Engineer")
    await digest_conn.add_job("b", title="Platform Engineer", notified=True)

    from notify.store import fetch_candidates

    before = await digest_conn.snapshot()
    await enrich_candidates(
        digest_conn,
        await fetch_candidates(digest_conn, ignore_notified=True),
        fetcher=await stub_fetcher({}),
    )
    after = await digest_conn.snapshot()

    assert [after[k]["notified"] for k in after] == [before[k]["notified"] for k in before]
    assert await digest_conn.sent_log() == []
