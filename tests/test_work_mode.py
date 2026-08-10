"""The work-arrangement parser, and the phrases it must refuse to be fooled by.

Two tiers, tested separately because they fail differently:

  work_mode_from_row   the ATS's own field. Cannot be wrong about a value it was
                       handed; can be wrong about WHICH key to read, which is why
                       the shapes here are copied from live payloads.
  detect_work_mode     prose. The dangerous one, and the direction of the danger
                       is the opposite of detect_pm_role's — a false `remote`
                       ADMITS a job by relaxing the city gate, so `remote` has to
                       be hard to convince and `onsite` is the resting state.

THE FIXTURES ARE REAL SENTENCES. Every "must not fire" string below is the kind
of line that appears in an ordinary Bengaluru-onsite backend JD; that is the
whole point of them, and inventing gentler ones would test nothing.
"""

from __future__ import annotations

import pytest

from notify.description import (
    DEFAULT_WORK_MODE,
    WORK_MODES,
    detect_work_mode,
    explain_work_mode,
    work_mode_from_row,
)

# --- tier 1: the vendor's own field ---------------------------------------
#
# Shapes verified live, one company each: ashby/atlan, lever/epifi,
# smartrecruiters/freshworks. The casing difference between ashby ("Remote") and
# lever ("remote") is real and is why the reader lowercases.


@pytest.mark.parametrize(
    "ats, row, expected",
    [
        ("ashby", {"workplaceType": "Remote", "isRemote": True}, "remote"),
        ("ashby", {"workplaceType": "Hybrid", "isRemote": False}, "hybrid"),
        ("ashby", {"workplaceType": "OnSite", "isRemote": False}, "onsite"),
        # Snowflake's board carries 207 rows with the field unset. Unset is not
        # onsite — it is "this vendor did not say", and the prose tier answers.
        ("ashby", {"workplaceType": None, "isRemote": None}, None),
        ("lever", {"workplaceType": "onsite"}, "onsite"),
        ("lever", {"workplaceType": "remote"}, "remote"),
        ("lever", {"workplaceType": "hybrid"}, "hybrid"),
        ("smartrecruiters", {"location": {"remote": True, "hybrid": False}}, "remote"),
        ("smartrecruiters", {"location": {"remote": False, "hybrid": True}}, "hybrid"),
        # Both false is a real answer rather than a silence: the vendor has a
        # field for each and set neither.
        ("smartrecruiters", {"location": {"remote": False, "hybrid": False}}, "onsite"),
        ("smartrecruiters", {"location": {}}, None),
        # The two ATSes that publish nothing at all. Checked so that a future
        # edit adding a guessed key here fails loudly.
        ("greenhouse", {"location": {"name": "Remote - US"}}, None),
        ("workday", {"jobPostingInfo": {"location": "Remote"}}, None),
        ("lever", {}, None),
        ("lever", None, None),
    ],
)
def test_the_vendor_field_is_read_not_inferred(ats, row, expected) -> None:
    assert work_mode_from_row(ats, row) == expected


def test_an_unknown_vendor_value_is_not_invented_into_a_known_one() -> None:
    """A fourth value is None here rather than being coerced to the nearest one.

    db/schema.sql puts no CHECK on the column precisely so a vendor writing
    something new is data to look at. The reader's job is to recognise the three
    it knows; guessing that "WFH" means remote is how a vocabulary drifts.
    """
    assert work_mode_from_row("lever", {"workplaceType": "flexible"}) is None
    assert work_mode_from_row("ashby", {"workplaceType": "WFH"}) is None


# --- tier 2: prose --------------------------------------------------------

BENGALURU_JD = (
    "About the role\n"
    "You will build and operate backend services for our payments platform.\n"
    "Requirements\n"
    "3 years of experience with Python and distributed systems.\n"
)


def test_a_description_that_states_nothing_is_onsite() -> None:
    """The specified rule: unspecified means onsite. A verdict, not a shrug."""
    assert detect_work_mode("Backend Engineer", BENGALURU_JD) == DEFAULT_WORK_MODE == "onsite"
    # ...and explain says so by returning no marker, which is what lets the
    # backfill report separate "read it, said nothing" from "matched a phrase".
    mode, marker, _ = explain_work_mode("Backend Engineer", BENGALURU_JD)
    assert (mode, marker) == ("onsite", None)


def test_no_description_at_all_is_None_and_not_onsite() -> None:
    """The read/unread distinction, which is the whole of why NULL is not backfilled.

    A failed fetch must not produce the same value as a successful read of a
    silent description. Both filter as onsite (notify.match.work_mode_of), and
    only one of them is a thing anybody observed.
    """
    for nothing in (None, "", "   ", 42, {}):
        assert detect_work_mode("Backend Engineer", nothing) is None


@pytest.mark.parametrize(
    "sentence",
    [
        "This role is fully remote.",
        "This position is 100% remote and always will be.",
        "You will work remotely, wherever you are.",
        "This is a remote role.",
        "Location: Remote",
        "The role is remote.",
        "This is an entirely remote position.",
        "You can work from anywhere.",
        "Workplace type: Remote",
    ],
)
def test_only_a_strong_unambiguous_signal_is_read_as_remote(sentence: str) -> None:
    """The strict half: an unmistakable statement that THIS posting is remote."""
    assert detect_work_mode("Backend Engineer", BENGALURU_JD + sentence) == "remote"


@pytest.mark.parametrize(
    "sentence",
    [
        # THE STRICTNESS DECISION, AS TESTS. Each of these is real JD language
        # that an earlier, looser draft of this parser read as remote. None of
        # them says this posting is remote, and under the asymmetry that governs
        # this file — a wrong onsite is a quiet miss, a wrong remote bypasses the
        # city gate — every one of them defaults to onsite instead.
        "We are a remote-first company.",
        "GitLab is an all-remote company with team members worldwide.",
        "This is a remote-friendly team.",
        "We offer a flexible working policy.",
        "WFH allowance of INR 25,000 is provided.",
        "Work from home on Fridays.",
        "Occasional remote work is possible.",
        "You may work remotely from time to time.",
        "We have remote roles across the company.",
        "Our teams are distributed across three continents.",
        "Some remote work is supported for this team.",
    ],
)
def test_weak_or_suggestive_language_does_not_earn_remote(sentence: str) -> None:
    assert detect_work_mode("Backend Engineer", BENGALURU_JD + sentence) == "onsite"


@pytest.mark.parametrize(
    "sentence",
    [
        "This is a hybrid role, based in our Bengaluru office.",
        "We follow a hybrid working model.",
        "Hybrid - 3 days in the office each week.",
        "You will spend 3 days a week in the office.",
        "In-office 2 days, the rest remote.",
        "Hybrid work arrangement, Bengaluru.",
    ],
)
def test_framed_hybrid_language_is_read_as_hybrid(sentence: str) -> None:
    assert detect_work_mode("Backend Engineer", BENGALURU_JD + sentence) == "hybrid"


def test_hybrid_wins_over_remote_when_a_posting_says_both() -> None:
    """The precedence that keeps a hybrid job from relaxing the city gate.

    A hybrid JD nearly always contains remote language, because describing a
    hybrid week means saying which days are remote. Reading that as fully-remote
    would exempt from the city test a posting that is explicitly attached to an
    office — the same reason navi-mumbai is scanned before mumbai in
    fetchers/normalize.py.
    """
    both = BENGALURU_JD + "A hybrid role: two days remote, three days in the office."
    assert detect_work_mode("Backend Engineer", both) == "hybrid"


# --- the guards, which are the reason this parser is allowed to run at all ---


@pytest.mark.parametrize(
    "sentence",
    [
        # The three named in the brief.
        "You will manage a remote team of eight engineers from our Bengaluru office.",
        "Experience debugging remote servers over SSH is required.",
        "Comfortable with remote debugging and profiling in production.",
        # The rest of the family, all ordinary engineering English.
        "You will work with remote colleagues across three time zones.",
        "Push your branch to the remote repository before opening a PR.",
        "Deep understanding of remote procedure calls and gRPC.",
        "Manage remote access policies for the fleet.",
        "Build remote monitoring for our edge devices.",
        "Experience with remote data replication between clusters.",
        "We support remote work for some teams.",
        "You will be leading a remote group of contractors.",
        "Familiarity with remote desktop tooling.",
        "Our remote-friendly culture is something we are proud of.",
        "Coordinate across remote offices in APAC.",
        "The service exposes a remote API endpoint for partners.",
        # FOUND BY THE DRY RUN, NOT BY REVIEW. This exact line is in Stripe's
        # "Software Engineer, Secrets Infrastructure" posting, whose location
        # field is the literal string "na", and it was the ONE job the first dry
        # run of this slice would have added to the operator's digest. It is a
        # requirement about the applicant's history that happens to be phrased
        # the same way a statement about the role would be.
        "Experience operating complex services on AWS. Experience in fully remote work environments.",
        "Proven track record in a 100% remote organisation.",
        "Comfortable working in a fully remote environment.",
        "You have thrived in remote settings before.",
    ],
)
def test_a_technical_or_incidental_remote_never_makes_a_job_remote(sentence: str) -> None:
    """THE FALSE-POSITIVE GUARDS, FIRING.

    Every sentence here contains the word "remote" and none of them says the
    posting is a remote job. A parser that read any of them as an arrangement
    would relax the city gate on an office role — and the failure is silent: the
    job simply turns up in a digest it should never have reached, looking exactly
    like a job that belongs there.
    """
    assert detect_work_mode("Backend Engineer", BENGALURU_JD + sentence) == "onsite"


@pytest.mark.parametrize(
    "sentence",
    [
        "You will design hybrid cloud architecture across AWS and on-prem.",
        "Experience building hybrid mobile applications in React Native.",
        "We use a hybrid model combining retrieval and generation.",
        "Hybrid search over vector and keyword indexes.",
        "Familiarity with hybrid encryption schemes.",
    ],
)
def test_a_technical_hybrid_never_makes_a_job_hybrid(sentence: str) -> None:
    """"Hybrid" is all over engineering prose that says nothing about where you sit.

    Less obvious than the remote family and just as real: "hybrid cloud" is in a
    large share of infrastructure JDs. Bare "hybrid" is therefore never enough,
    which is what the framed patterns enforce.
    """
    assert detect_work_mode("Backend Engineer", BENGALURU_JD + sentence) == "onsite"


def test_a_veto_does_not_swallow_a_real_statement_elsewhere_in_the_text() -> None:
    """Masking, not a whole-text veto — and this is the case that tells them apart.

    A description can perfectly well mention remote servers AND state that the
    role is remote. A veto that rejected the whole document on the first
    technical hit would lose the second sentence; a scan that stopped at the
    first "remote" would call an office job remote. Same-length masking is what
    lets both sentences be read correctly in the same pass.
    """
    text = (
        BENGALURU_JD
        + "You will debug remote servers and manage a remote team.\n"
        + "This role is fully remote.\n"
    )
    assert detect_work_mode("Backend Engineer", text) == "remote"

    # And the candidate-history veto must not swallow a real statement either.
    # The bound is a sentence, so a requirements bullet cannot reach past its own
    # full stop and silence the line after it.
    assert (
        detect_work_mode(
            "Backend Engineer",
            BENGALURU_JD
            + "Experience in fully remote work environments. This role is fully remote.\n",
        )
        == "remote"
    )


def test_the_title_answers_without_framing_and_the_snippet_shows_why() -> None:
    """A title needs no framing, exactly as _PM_TITLE_RE does not.

    "Software Engineer (Remote)" is not discussing remote servers. The anchoring
    is on the bracket or dash a title actually uses, so a role genuinely ABOUT
    remote things keeps its own name.
    """
    assert detect_work_mode("Software Engineer (Remote)", BENGALURU_JD) == "remote"
    assert detect_work_mode("Backend Engineer - Remote", BENGALURU_JD) == "remote"
    assert detect_work_mode("Remote Sensing Engineer", BENGALURU_JD) == "onsite"

    mode, marker, snippet = explain_work_mode("Backend Engineer", BENGALURU_JD + "This role is fully remote.")
    assert mode == "remote"
    assert marker and "remote" in marker.lower()
    assert snippet and "fully remote" in snippet


def test_every_verdict_is_one_of_the_three_or_None() -> None:
    """The enum, asserted rather than assumed.

    jobs.work_mode has no CHECK (see db/schema.sql), so nothing in the database
    would refuse a fourth value — which makes this the place the vocabulary is
    actually enforced, and the reason notify.match.WORK_MODE_LABELS can be a
    plain membership test.
    """
    samples = [
        BENGALURU_JD,
        BENGALURU_JD + "This role is fully remote.",
        BENGALURU_JD + "A hybrid role, 3 days in the office.",
        BENGALURU_JD + "You will manage a remote team.",
    ]
    for text in samples:
        assert detect_work_mode("Backend Engineer", text) in WORK_MODES
