"""The salary slice: the parser, its guards, the band gate, and the write path.

FOUR SECTIONS, AND THE SECOND IS THE ONE THAT EARNS ITS KEEP.

§1 is the parser doing its job — the shapes an Indian posting states pay in, all
   normalised to LPA.
§2 is the parser REFUSING, and it is the biggest section on purpose. Measured
   against the live board before any of this was written, 36 of 36 candidate
   descriptions stated no salary and 22 of them contained the words salary,
   compensation or CTC. So on today's data every single verdict this parser
   produces is a refusal, and a refusal is the only behaviour real traffic can
   exercise. Every case in §2 is a verbatim string from that live sample or the
   shape one of them generalises to.
§3 is the gate: overlap, fail-open on a blank selection, and the unpriced job
   that shows regardless.
§4 is the rail: what the enrichment pass writes, and the salary_checked
   distinction that keeps "no pay stated" apart from "nobody looked".

THE ASYMMETRY EVERY TEST HERE IS ARRANGED AROUND. A missed salary costs one
unfiltered job in a digest. A WRONG salary puts a job in a band nobody ticked and
DELETES it — silently, with the row still on the board looking fine. So the
parser's failure direction is silence, and §2 is where that is proven rather than
asserted in a docstring.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from notify.description import (
    SALARY_VETOES,
    detect_salary,
    explain_salary,
    salary_from_row,
)
from notify.match import (
    DEFAULT_SALARY_BANDS,
    SALARY_BAND_BOUNDS,
    SALARY_BAND_LABELS,
    Preferences,
    is_match,
    salary_of,
    salary_ok,
)
from notify.store import save_description_facts

pytestmark = pytest.mark.anyio


# --- §1 the shapes a posting states pay in ---------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        # Lakhs, stated as such. The canonical Indian form.
        ("Compensation: 18-24 LPA", (18, 24)),
        ("We offer 22 LPA for this role.", (22, 22)),
        ("CTC of 15 lakhs per annum", (15, 15)),
        ("Package: 12.5 lacs", (Decimal("12.5"), Decimal("12.5"))),
        ("Budgeted at 18 to 24 lakhs", (18, 24)),
        # Raw rupees, Indian digit grouping and western, both stripped the same.
        ("₹18,00,000 - ₹24,00,000 per annum", (18, 24)),
        ("INR 1,800,000 annually", (18, 18)),
        ("Rs. 2500000", (25, 25)),
        # The bare L suffix, which is accepted ONLY with a currency marker.
        ("₹18L", (18, 18)),
        # Crore.
        ("CTC ₹1.5 crore", (150, 150)),
        # Monthly, annualised by twelve.
        ("Salary: ₹80,000/month", (Decimal("9.6"), Decimal("9.6"))),
        ("₹1,50,000 per month", (18, 18)),
        ("₹80k per month", (Decimal("9.6"), Decimal("9.6"))),
        # A range with only the trailing unit stated.
        ("between 20 and 30 LPA", (20, 30)),
    ],
)
def test_the_parser_normalises_every_stated_form_to_lpa(text, expected) -> None:
    assert detect_salary(text) == (Decimal(expected[0]), Decimal(expected[1]))


def test_an_open_ended_figure_keeps_both_ends_equal() -> None:
    """"18 LPA+" is a point, not a range, and the plus is not evidence of a top.

    A single figure sets both ends to itself. The alternative — reading "+" as an
    unbounded ceiling — would make one posting overlap every band above it on the
    strength of a punctuation mark.
    """
    assert detect_salary("18 LPA+") == (Decimal(18), Decimal(18))


def test_a_restatement_of_one_figure_is_not_two_figures() -> None:
    """"₹18,00,000 – ₹24,00,000 (18-24 LPA)" is one fact written twice.

    Both forms normalise to the same pair, so the dedupe in explain_salary sees
    one entry and the ambiguity rule below does not fire. Without the
    normalisation-then-dedupe order this would read as a disagreement and refuse
    a posting that could not have been clearer.
    """
    assert detect_salary("₹18,00,000 – ₹24,00,000 (18-24 LPA)") == (Decimal(18), Decimal(24))


def test_the_evidence_travels_with_the_verdict() -> None:
    """explain_salary returns the marker and a snippet, for the backfill sample.

    The reviewable unit is the phrase, not the row — the same instrument
    scripts/relabel_cities.py's residue report and the work-mode marker report
    use. A person cannot audit a hundred numbers, but they can audit the dozen
    sentences the numbers came out of.
    """
    read = explain_salary("Great team. Compensation: 18-24 LPA, reviewed annually.")
    assert (read.low, read.high) == (Decimal(18), Decimal(24))
    assert "18-24 LPA" in read.marker
    assert "Compensation" in read.snippet


# --- §2 the parser refusing --------------------------------------------------
#
# THE ANCHOR CASES FIRST. These carry no currency and no unit, so no rule in the
# module can see them at all — which is a stronger guarantee than a veto list,
# because a list can be incomplete and a missing anchor cannot. Every string here
# is verbatim from the live candidate set.


@pytest.mark.parametrize(
    "shape, text",
    [
        ("headcount", "Our 30,000+ employees worldwide are creating the future."),
        ("headcount", "currently employs around 200,000 people in 56 countries"),
        ("headcount", "you'll be part of a 3000+ strong team building not just products"),
        ("headcount", "supported by a passionate team of 1,700+ Zetanauts across India"),
        ("customers", "Our platform is trusted by over 2,500 of the world's most innovative companies"),
        ("customers", "Trusted by 30,000+ brands and leading publishers"),
        ("ordinal", "the same way to the 200,000th candidate as to the first"),
        ("founding year", "Founded in 1871, the technology company offers safe, efficient"),
        ("calendar year", "Winner of the Celent Model Bank Award for Payments Innovation 2024"),
        ("phone number", "email accommodations@example.test or call +1 408-536-3015"),
        ("requisition id", "Requisition R168336, posted to the Bengaluru office"),
        ("years of experience", "5 years of experience in distributed systems required"),
        ("years of experience", "3-6 years in data/platform engineering"),
    ],
)
def test_a_number_without_a_currency_or_a_unit_is_never_a_salary(shape, text) -> None:
    """The structural guard: no ₹, no lakh, no salary. Whatever the number means.

    This is the guard that removes most of the false-positive surface by
    construction. A parser that read bare numbers would find a wage in every one
    of these thirteen real strings; this one cannot see them.
    """
    assert detect_salary(text) == (None, None), f"{shape} was read as pay"


@pytest.mark.parametrize(
    "veto, text",
    [
        # EQUITY. The number beside the wage, and usually the bigger one.
        ("equity/ESOP", "₹50,00,000 ESOP grant over four years"),
        ("equity/ESOP", "ESOPs worth ₹20 lakhs on joining"),
        ("equity/ESOP", "₹15,00,000 in stock options"),
        # THE COMPANY'S MONEY, not the offer's.
        ("funding/revenue/valuation", "we raised ₹40 lakh in our seed round"),
        ("funding/revenue/valuation", "Continental generated sales of €41.4 billion"),
        ("funding/revenue/valuation", "we reached a valuation of $2 billion in 2025"),
        ("funding/revenue/valuation", "₹1,200 crore in annual revenue"),
        # PERKS. Real rupee figures in a section formatted exactly like a
        # compensation section — the _NOT_A_CAREER problem, one column over.
        ("benefit/bonus/insurance", "₹5,00,000 medical insurance cover for your family"),
        ("benefit/bonus/insurance", "a joining bonus of ₹1 lakh"),
        ("benefit/bonus/insurance", "₹50,000 relocation allowance"),
        ("benefit/bonus/insurance", "an annual learning budget of ₹25,000"),
        # FOREIGN MONEY. Masked, never converted — an FX rate would be frozen
        # into a column nothing re-derives.
        ("foreign currency", "The range is $180,000 - $220,000 USD"),
        ("foreign currency", "Annual Salary: €220.000"),
        ("foreign currency", "£90,000 to £110,000 depending on experience"),
        # THE APPLICATION FORM. Guaranteed present on every Greenhouse row,
        # because _greenhouse_description joins questions[].label into the text.
        ("application-form question", "What are your salary expectations?"),
        (
            "application-form question",
            "Current Compensation (Please mention breakup of Fixed + Variable + ESOPs)",
        ),
        ("application-form question", "Expected CTC: ₹24,00,000"),
        # A RATE IS NOT AN ANNUAL WAGE, and annualising one needs an assumption.
        ("hourly/daily rate", "Rate: ₹500/hour"),
        ("hourly/daily rate", "₹8,000 per day for the engagement"),
    ],
)
def test_money_that_is_not_this_job_s_pay_is_vetoed(veto, text) -> None:
    """Real currency figures that a naive parser WOULD read, and the guard firing.

    Two assertions rather than one, deliberately. The first is the behaviour that
    matters — no salary comes out. The second names the veto that produced it, so
    a case that starts passing for an unrelated reason (a regex that stopped
    matching for the wrong reason, an anchor that quietly narrowed) fails here
    instead of silently becoming a test of nothing.
    """
    assert detect_salary(text) == (None, None), f"{veto!r} case was read as pay"
    assert veto in explain_salary(text).vetoes, (
        f"no salary came out, but the {veto!r} guard is not what stopped it — "
        f"vetoes that fired: {explain_salary(text).vetoes}"
    )


def test_every_veto_has_at_least_one_case_above() -> None:
    """The parametrisation is pinned to the veto list, literally.

    tests/test_city_selection.py learned this the hard way: two tests parametrized
    over a constant silently shrank when the constant did, and covered nothing
    while staying green. So the veto names are asserted as a literal set here, and
    adding a guard without a case fails this rather than passing quietly.
    """
    assert {name for name, _ in SALARY_VETOES} == {
        "application-form question",
        "equity/ESOP",
        "funding/revenue/valuation",
        "benefit/bonus/insurance",
        "foreign currency",
        "hourly/daily rate",
    }


def test_a_veto_masks_its_own_figure_and_leaves_a_real_one_standing() -> None:
    """The reason _mask exists rather than a whole-text veto.

    A description can state an ESOP grant in one sentence and a wage in the next,
    and both a whole-text veto and a first-hit-wins scan would get one of the two
    wrong. Same-length masking is what lets this sentence answer 18.
    """
    text = "Base is 18 LPA in Bengaluru; we also grant ESOPs worth ₹20 lakhs."
    assert detect_salary(text) == (Decimal(18), Decimal(18))


def test_two_disagreeing_figures_mean_nothing_was_established() -> None:
    """The one place this module deliberately inverts detect_min_experience.

    That one takes min(found) because the smallest minimum is the answer that
    KEEPS the job, and a keep is the safe error. There is no safe number here: any
    figure can put the job in a band nobody ticked and delete it, while silence
    shows it. So ambiguity resolves to silence.
    """
    assert detect_salary("The role pays 18 LPA. Elsewhere we list it as 35 LPA.") == (None, None)


@pytest.mark.parametrize(
    "text, why",
    [
        ("₹500 crore raised", "50,000 LPA — above the ceiling"),
        ("₹5,000 for the referral", "0.05 LPA — below the floor"),
        ("18 LPA per month", "LPA already means per annum; a monthly one is not a wage"),
    ],
)
def test_an_implausible_figure_is_refused(text, why) -> None:
    """The bounds are a guard, not a formality — this is what kills funding lines.

    A ₹500 crore round normalises to 50,000 LPA and fails on arithmetic even if
    every veto above had missed it. Belt and braces, and the braces are the ones
    that hold when a new phrasing appears that no veto anticipated.
    """
    assert detect_salary(text) == (None, None), why


def test_lakh_crore_is_one_unit_and_is_not_lakhs() -> None:
    """THE FALSE POSITIVE THE FIRST LIVE DRY RUN CAUGHT, pinned verbatim.

    Five PhonePe postings were priced at 150 LPA from this sentence. The lakh
    pattern matched "INR 150 lakh", stopped, and never saw the word that changes
    the magnitude by seven orders of ten — so a payments company's annual volume
    became a plausible senior salary purely by being truncated into one.

    It is the exact failure the dry run exists to surface: every guard in §2 was
    working, the anchor was satisfied honestly (there really is an INR and a
    lakh), the figure landed inside the plausibility band, and only a person
    reading the snippet beside the number could tell it was wrong.

    The fix is the number's own shape rather than a veto, which is why this test
    lives here and not in the veto block: `150 lakh crore` now matches NOTHING,
    because the lakh forms refuse a trailing `crore` and the crore form needs its
    digits adjacent. Belt and braces — the funding veto also learned `TPV` — but
    the shape is what holds if a new metric name appears that no veto anticipated.
    """
    phonepe = (
        "PhonePe processes 310 million (31 crore) transactions daily with an "
        "Annualized Total Payment Value (TPV) of over INR 150 lakh crore. "
        "PhonePe's portfolio of businesses includes the distribution of financial products."
    )
    assert detect_salary(phonepe) == (None, None)

    # And the compound in isolation, so a future rewrite that reintroduces the
    # truncation fails here with a one-line reason rather than inside a paragraph.
    assert detect_salary("a TPV of over INR 150 lakh crore") == (None, None)
    assert detect_salary("₹5 lakh crore in assets") == (None, None)

    # The ordinary units still work — the lookahead must not have eaten them.
    assert detect_salary("CTC of 15 lakhs per annum") == (Decimal(15), Decimal(15))
    assert detect_salary("CTC ₹1.5 crore") == (Decimal(150), Decimal(150))


def test_nothing_at_all_is_not_an_error() -> None:
    for empty in (None, "", "   ", 42, ["a list"]):
        assert detect_salary(empty) == (None, None)


# --- §3 the gate -------------------------------------------------------------


def test_the_bands_are_contiguous_and_gapless() -> None:
    """Every LPA figure from 0 upward falls in exactly one band.

    A gap would be a wage nobody can tick and an overlap would be a wage two
    people tick differently, and both are silent — the job simply behaves oddly
    for one account. Asserted over the bounds rather than eyeballed, because the
    labels look contiguous whatever the numbers say.
    """
    assert list(SALARY_BAND_BOUNDS) == list(SALARY_BAND_LABELS)
    bounds = [SALARY_BAND_BOUNDS[label] for label in SALARY_BAND_LABELS]
    assert bounds[0][0] == 0, "the ladder must start at zero"
    assert bounds[-1][1] is None, "the top band must be unbounded"
    for (_, high), (low, _) in zip(bounds, bounds[1:]):
        assert high == low, "a gap or an overlap between adjacent bands"


def priced(low, high, **extra):
    """A matchable row at a stated price. Everything else passes every other test.

    A plain dict rather than conftest.job, which builds a FETCHER-shaped row —
    the matchers read a Mapping of jobs columns, which is what asyncpg hands them.
    """
    return {
        "canonical_id": "syn-1",
        "title": "Backend Engineer",
        "city": "blr",
        "location": "Bengaluru, India",
        "work_mode": None,
        "min_experience": None,
        "is_pm_role": False,
        "salary_min": low,
        "salary_max": high,
        "salary_checked": True,
        **extra,
    }


def unpriced(**extra):
    """The state of every row on the live board: nothing known about pay."""
    return priced(None, None, salary_checked=False, **extra)


ALL_BANDS = Preferences(salary_bands=DEFAULT_SALARY_BANDS)


@pytest.mark.parametrize(
    "low, high, band, expect",
    [
        # The plain overlap.
        (18, 24, "15-25", True),
        (18, 24, "10-15", False),
        (18, 24, "<10", False),
        (18, 24, "25-40", False),
        # OVERLAP, NOT CONTAINMENT. A 12-18 posting partly meets both bands it
        # straddles, and a person who ticked either should see it.
        (12, 18, "10-15", True),
        (12, 18, "15-25", True),
        # The half-open boundary, from both sides. 10.0 is the first LPA of
        # `10-15` and is NOT the last of `<10`.
        (10, 10, "<10", False),
        (10, 10, "10-15", True),
        (Decimal("9.99"), Decimal("9.99"), "<10", True),
        # The unbounded top band.
        (55, 60, "50+", True),
        (55, 60, "40-50", False),
        (48, 52, "50+", True),
    ],
)
def test_a_priced_job_matches_a_band_it_overlaps(low, high, band, expect) -> None:
    prefs = Preferences(salary_bands=(band,))
    assert salary_ok(priced(low, high), prefs) is expect


def test_an_unpriced_job_shows_regardless_of_bands() -> None:
    """The branch that covers 100% of the live board, and the one that must not move.

    Every stored row is unpriced and will stay that way — the parser refuses on
    every real description it has been shown. So this branch IS the behaviour of
    the salary gate in production, and a change that made an unpriced job depend
    on the band selection would empty every digest on this install at once.
    """
    narrow = Preferences(salary_bands=("<10",))
    assert salary_ok(unpriced(), narrow) is True
    assert salary_ok(priced(None, None), narrow) is True


def test_a_blank_selection_filters_nothing_and_this_is_deliberate() -> None:
    """FAIL-OPEN — the one field in this family that does not match nothing.

    An unconfigured experience range, a blank city answer and an empty work_modes
    all match NOTHING. This does the opposite, and notify.match.salary_ok carries
    the four-part argument. The short version: the rule already shows a job whose
    EMPLOYER said nothing about pay, so hiding one because the READER said nothing
    would mean silence on one side of the same question admitting a job and
    silence on the other deleting it.
    """
    blank = Preferences(salary_bands=())
    assert salary_ok(priced(18, 24), blank) is True
    assert salary_ok(priced(150, 150), blank) is True
    assert salary_ok(unpriced(), blank) is True


def test_an_unrecognised_band_matches_nothing_rather_than_raising() -> None:
    """A hand-edited row is data to look at, not a crash mid-digest.

    Same posture as an unrecognised work mode, which simply fails its membership
    test. The job is still shown if ANY recognised band overlaps, so a row
    carrying one good band and one typo behaves as though the typo were absent.
    """
    assert salary_ok(priced(18, 24), Preferences(salary_bands=("18-24L",))) is False
    assert salary_ok(priced(18, 24), Preferences(salary_bands=("18-24L", "15-25"))) is True


def test_the_operator_s_six_bands_are_a_no_op() -> None:
    """All six selected means every priced job passes, and every unpriced one too.

    This is the live row (scripts/backfill_onboarding_prefs.py wrote all six), so
    it is the property that keeps match-parity at 34 across this slice: the gate
    cannot subtract anything from an account that ticked the whole ladder.
    """
    for low, high in ((1, 1), (9, 11), (18, 24), (49, 51), (150, 200), (None, None)):
        assert salary_ok(priced(low, high), ALL_BANDS) is True


def test_salary_of_reads_both_driver_types_and_junk() -> None:
    """asyncpg hands back Decimal, the SQLite mirror hands back float.

    Comparing the two directly is a TypeError in the middle of a digest run, so
    both go through Decimal(str(...)) — the accommodation Preferences._as_tuple
    already makes for TEXT[] arriving as JSON.
    """
    assert salary_of({"salary_min": Decimal("18"), "salary_max": Decimal("24")}) == (
        Decimal(18),
        Decimal(24),
    )
    assert salary_of({"salary_min": 18.0, "salary_max": 24.0}) == (Decimal(18), Decimal(24))
    assert salary_of({"salary_min": "nonsense", "salary_max": None}) == (None, None)
    assert salary_of({}) == (None, None)


def test_the_gate_is_wired_into_is_match_and_can_drop_a_job() -> None:
    """End to end: a job that passes every other test, dropped for its pay alone.

    The point of a separate assertion is that salary_ok being correct proves
    nothing about is_match calling it — which is exactly the gap
    `npm run match-parity`'s synthetic probes close on the TypeScript side.
    """
    prefs = Preferences(
        selected_cities=("blr",),
        keywords=("backend",),
        exclusions=(),
        exp_range_low=0,
        exp_range_high=2,
        show_no_experience_jobs=True,
        work_modes=("onsite", "hybrid", "remote"),
        salary_bands=("<10",),
    )
    assert is_match(priced(None, None), prefs) is True, "unpriced still matches"
    assert is_match(priced(5, 8), prefs) is True, "inside the ticked band"
    assert is_match(priced(18, 24), prefs) is False, "priced outside every ticked band"


# --- §4 the rail -------------------------------------------------------------


async def test_the_enrichment_write_stores_the_pair_and_the_flag(conn) -> None:
    """save_description_facts persists all three, on the row it names and no other."""
    await conn.add_company(1, "Testco")
    await conn.add_job("j1", title="Backend Engineer")
    await conn.add_job("j2", title="Backend Engineer")

    await save_description_facts(conn, [("j1", 2, False, False, "onsite", 18, 24, True)])

    rows = {r["canonical_id"]: r for r in await conn.fetch("SELECT * FROM jobs")}
    assert (rows["j1"]["salary_min"], rows["j1"]["salary_max"]) == (18, 24)
    assert rows["j1"]["salary_checked"] == 1
    assert (rows["j2"]["salary_min"], rows["j2"]["salary_max"]) == (None, None)
    assert rows["j2"]["salary_checked"] == 0


async def test_read_and_found_nothing_is_not_never_looked(conn) -> None:
    """THE ONE-WAY DOOR THIS COLUMN EXISTS TO KEEP OPEN.

    Both rows below end with salary_min NULL, and before salary_checked existed
    they would have been indistinguishable — one column carrying two facts, which
    is jobs.notified with a decimal point and cost four sub-phases to undo there.

    The distinction is preserved ON THE ROW and deliberately collapsed IN THE
    FILTER: salary_ok shows both, because an unpriced job is visible either way.
    That is the point. It is kept where it is free, not enforced where it would
    change an inbox.
    """
    await conn.add_company(1, "Testco")
    await conn.add_job("read-it", title="Backend Engineer")
    await conn.add_job("never-looked", title="Backend Engineer")

    await save_description_facts(conn, [("read-it", None, False, False, "onsite", None, None, True)])

    rows = {r["canonical_id"]: r for r in await conn.fetch("SELECT * FROM jobs")}
    assert rows["read-it"]["salary_min"] is None
    assert rows["read-it"]["salary_checked"] == 1, "we looked; this posting states no pay"
    assert rows["never-looked"]["salary_min"] is None
    assert rows["never-looked"]["salary_checked"] == 0, "nobody has looked"

    # And the filter cannot tell them apart, which is the specified behaviour.
    # dict(row) because the matchers read a Mapping and sqlite3.Row is not one;
    # asyncpg hands them a real Record, which is.
    for row in rows.values():
        assert salary_ok(dict(row), Preferences(salary_bands=("50+",))) is True


async def test_a_failed_fetch_does_not_claim_a_parser_read_it(conn) -> None:
    """desc_checked and salary_checked differ on exactly the failed rows.

    desc_checked means "we asked", which is true even when nothing came back —
    that is why it is a literal TRUE in the statement. salary_checked means "a
    parser saw a description", which a failed fetch is precisely not. Writing it
    as a literal would have marked every desc_fetch_failed row parsed-and-empty.
    """
    await conn.add_company(1, "Testco")
    await conn.add_job("dead", title="Backend Engineer")

    await save_description_facts(conn, [("dead", None, False, True, None, None, None, False)])

    [row] = await conn.fetch("SELECT * FROM jobs WHERE canonical_id = 'dead'")
    assert row["desc_checked"] == 1, "we asked"
    assert row["desc_fetch_failed"] == 1, "and got nothing"
    assert row["salary_checked"] == 0, "so no parser has seen this posting's prose"


# --- the structural tier -----------------------------------------------------


def test_the_vendor_s_own_field_is_read_where_one_exists() -> None:
    """ashby and recruitee publish a comp field; the other seven do not.

    Verified live rather than from documentation — the discipline the Workday
    country facet earned. Ashby's only appears when the board URL carries
    ?includeCompensation=true, which is why notify/description._BOARD_URLS spells
    it differently from fetchers/tier1.py's copy.
    """
    ashby_row = {
        "compensation": {
            "summaryComponents": [
                {
                    "compensationType": "Salary",
                    "interval": "1 YEAR",
                    "currencyCode": "INR",
                    "minValue": 1800000,
                    "maxValue": 2400000,
                }
            ]
        }
    }
    assert salary_from_row("ashby", ashby_row) == (Decimal(18), Decimal(24))

    recruitee_row = {"salary": {"min": 1500000, "max": 2000000, "period": "year", "currency": "INR"}}
    assert salary_from_row("recruitee", recruitee_row) == (Decimal(15), Decimal(20))

    for ats in ("greenhouse", "lever", "smartrecruiters", "workday", "workable"):
        assert salary_from_row(ats, ashby_row) == (None, None)


def test_a_foreign_vendor_figure_is_refused_like_a_foreign_prose_one() -> None:
    """The structural tier does not get to skip the no-conversion rule.

    A vendor stating $180,000 is stating a fact this tool cannot turn into a point
    on an Indian band ladder without freezing an exchange rate into a column
    nothing re-derives — and the arithmetic WORKS, which is what makes it
    dangerous: it would land a Seattle posting in `50+` looking like the best job
    on the board.
    """
    usd = {
        "compensation": {
            "summaryComponents": [
                {
                    "compensationType": "Salary",
                    "interval": "1 YEAR",
                    "currencyCode": "USD",
                    "minValue": 180000,
                    "maxValue": 220000,
                }
            ]
        }
    }
    assert salary_from_row("ashby", usd) == (None, None)

    equity = {
        "compensation": {
            "summaryComponents": [
                {
                    "compensationType": "Equity",
                    "interval": "1 YEAR",
                    "currencyCode": "INR",
                    "minValue": 5000000,
                    "maxValue": 5000000,
                }
            ]
        }
    }
    assert salary_from_row("ashby", equity) == (None, None), "equity is not salary here either"


def test_an_absent_or_empty_vendor_field_is_silence() -> None:
    """Which is the state of every live ashby board — both tenants have the
    display switched off, so `compensation` is present and its tiers are empty."""
    assert salary_from_row("ashby", {"compensation": {"summaryComponents": []}}) == (None, None)
    assert salary_from_row("ashby", {}) == (None, None)
    assert salary_from_row("ashby", None) == (None, None)
    assert salary_from_row("recruitee", {"salary": {"min": None, "max": None,
                                                    "period": None, "currency": None}}) == (None, None)
