"""Phase 3 Step 0 added twelve columns and changed nothing. This is the proof.

A migration whose entire claim is "no behaviour change" has a verification
problem: the way it fails is that nothing happens differently *today* and a
column quietly acquires a reader six weeks later, in the one code path nobody
re-read. So this file asserts the claim in the direction it can actually be
broken — not "the digest still works" (it would, either way) but "no module that
decides anything so much as NAMES one of these columns".

Four separate assertions, because they fail for four different reasons:

  * the schema really declares the twelve columns, with the defaults specced
    (the expand half happened at all);
  * no filter loader, matcher or badge rule mentions any of them, in either
    language (nothing reads them);
  * the two statements that WRITE jobs mention none of the three job columns
    (nothing writes them, so the crawl cannot fill them in by accident);
  * the matcher's value object still carries exactly four fields, so a column
    cannot reach is_match even if some future loader did select it.

The textual assertions are the same idiom tests/test_applied_column.py and
tests/test_dashboard_notified.py use, and for the same reason: a rule about
which columns a statement is allowed to name is checked against the statement's
text or it is not checked at all.

WHAT THIS FILE DELIBERATELY DOES NOT PROVE. It runs against the SQLite mirror
in conftest.py and against source text. Neither is evidence that the ALTERs
reached live Postgres — scripts/init_db.py's EXPECTED_COLUMNS is what asserts
that, against information_schema, on the database that matters.
"""

from __future__ import annotations

import dataclasses
import io
import re
import tokenize
from pathlib import Path

import pytest

from crawler.store import UPSERT_SEEN
from notify.match import Preferences, is_match
from notify.store import (
    CANDIDATES_SQL,
    PREFERENCES_SQL,
    SAVE_DESCRIPTION_FACTS_SQL,
    load_preferences,
)
from tests.conftest import TEST_USER_ID

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCHEMA = (PROJECT_ROOT / "db" / "schema.sql").read_text(encoding="utf-8")

# The nine on preferences, as (column, sql type, default-or-None).
PREFERENCE_COLUMNS = [
    ("exp_range_low", "INT", None),
    ("exp_range_high", "INT", None),
    ("years_of_experience_held", "INT", None),
    ("show_no_experience_jobs", "BOOLEAN", "FALSE"),
    ("selected_cities", "TEXT[]", "'{}'"),
    ("all_india", "BOOLEAN", "FALSE"),
    ("work_modes", "TEXT[]", "'{}'"),
    ("salary_bands", "TEXT[]", "'{}'"),
    ("onboarding_completed", "BOOLEAN", "FALSE"),
]

# The three on jobs. All nullable with no default — NULL is "not yet parsed".
JOB_COLUMNS = [
    ("work_mode", "TEXT"),
    ("salary_min", "NUMERIC"),
    ("salary_max", "NUMERIC"),
]

NEW_COLUMNS = [name for name, *_ in PREFERENCE_COLUMNS] + [name for name, _ in JOB_COLUMNS]

# THE COLUMNS A FEATURE STEP HAS WIRED UP, and why this list exists rather than
# the sweep below simply being deleted.
#
# Step 0 created twelve columns with no readers and asserted that none of them
# had one. That assertion was never meant to hold forever — it holds until the
# FEATURE step for a given column arrives, which is the step that owns the
# migration proof for it. Phase 3 Slice 1 was that step for onboarding_completed,
# which lib/preferences.savePreferences sets when a person saves their own filter
# for the first time.
#
# PHASE 3 SLICE 2 ADDS FOUR MORE, and they divide in a way worth keeping straight:
#
#   exp_range_low            read by notify.match.experience_excluded, its
#   exp_range_high           TypeScript port, and the SQL prefilter in
#   show_no_experience_jobs  lib/jobs.experienceClause. These genuinely decide.
#
#   years_of_experience_held informational. It appears in lib/preferences.ts and
#                            lib/labels.ts because a person edits it on the
#                            screen, and it must reach NO matcher — which the
#                            sweep can no longer assert for it, so
#                            test_the_informational_column_reaches_no_matcher
#                            below replaces that coverage rather than losing it.
#
# So the guard narrows by five names and goes on protecting the other seven. The
# alternative — dropping the test now that it has fired — would retire the whole
# guarantee on the day the first column was wired, which is the day it starts
# being worth something.
#
# PHASE 3, CITIES SLICE B ADDS TWO MORE, and they are the first pair whose wiring
# is a REPLACEMENT rather than an addition. selected_cities and all_india are the
# city gate now (notify.match.city_ok and its port); `cities` is the legacy column
# they replaced, still selected and still written for one slice so the deployed
# digest and a rollback both keep working. The CONTRACT slice drops it.
#
# jobs.work_mode JOINS THEM WITH AN ASTERISK, and it is the one entry here that is
# named by a matcher without being able to decide anything. The remote branch in
# notify.match.city_ok is built now and is INERT: the parser that fills the column
# is the next slice, so work_mode is NULL on all 9,703 stored rows and
# notify.match.is_remote returns False for every one of them.
#
# It has to be exempted from the sweep either way — match.py names it, so the sweep
# would fail — and the guarantee that exemption gives up is replaced rather than
# dropped, by two things that are both STRONGER than "no file mentions the name":
# test_the_job_write_paths_name_none_of_the_new_job_columns below (unchanged, and
# still the reason the column is honestly NULL — nothing can write it), and
# test_the_remote_branch_is_built_but_inert (new, behavioural: it asserts the
# branch exists AND that an unparsed row cannot reach it). The second is what
# fails the day somebody starts filling the column in without this file being
# updated to say so.
#
# THE WORK-MODE SLICE ADDS work_modes AND PROMOTES work_mode FROM ASTERISK TO
# ORDINARY. jobs.work_mode is now WRITTEN — by notify/store.SAVE_DESCRIPTION_FACTS
# _SQL, on the enrichment rail beside is_pm_role — and preferences.work_modes is
# now READ, by notify.match.work_mode_ok and its port. Both decide.
#
# TWO GUARANTEES CHANGE HANDS HERE, and neither is dropped:
#
#   * test_the_job_write_paths_name_none_of_the_new_job_columns can no longer
#     cover work_mode, because the whole point is that one statement now writes
#     it. It narrows to salary_min/salary_max and gains an explicit assertion
#     that the OTHER write path — crawler.store.UPSERT_SEEN — still does not name
#     work_mode. That is the one that matters: the crawl must not be able to
#     stamp an arrangement it has no evidence for on every re-sighting.
#   * test_the_remote_branch_is_built_but_inert asserted a fact that has now
#     deliberately stopped being true. It is replaced by
#     test_the_remote_branch_is_live_and_still_india_only, which keeps every
#     assertion that is still meaningful and drops only the inertness.
# THE SALARY SLICE WIRES UP THE LAST THREE, and STILL_UNWIRED becomes empty for
# the first time since Step 0 created these columns. That is not a reason to
# delete the sweep: the list below is what the NEXT speculative column will be
# added to, and a guard removed the day it stops having work to do is a guard
# that has to be rebuilt from memory the day it is needed again.
#
# jobs.salary_min / salary_max are written by notify/store.SAVE_DESCRIPTION_FACTS
# _SQL on the enrichment rail beside work_mode, and read by notify.match.salary_ok
# and its port. preferences.salary_bands is read by the same pair.
#
# THE SAME TWO GUARANTEES CHANGE HANDS AS FOR work_mode, and neither is dropped:
#
#   * test_the_job_write_paths_name_none_of_the_new_job_columns can no longer
#     cover the salary pair, so it narrows to the assertion that actually matters
#     and that survives every future slice — crawler.store.UPSERT_SEEN names
#     NONE of these columns. That is the hourly statement: it runs on every
#     re-sighting of every row, and a crawl able to stamp pay (or an arrangement)
#     from a board payload would overwrite a description-derived verdict with a
#     guess, every hour, forever.
#   * "the column is honestly NULL because nothing writes it" is replaced by
#     test_an_unpriced_job_shows_regardless_of_bands and the salary_checked
#     distinction — the column is now NULL because a parser looked and found
#     nothing, which db/schema.sql explains is a different fact and needed its own
#     column to stay one.
#
# jobs.salary_checked is NOT in NEW_COLUMNS at all: it is the salary slice's own
# column rather than one of Step 0's twelve, and it arrived already wired. It is
# named here only so a reader looking for it finds out where it went.
WIRED_UP = {
    "onboarding_completed",
    "exp_range_low",
    "exp_range_high",
    "show_no_experience_jobs",
    "years_of_experience_held",
    "selected_cities",
    "all_india",
    "work_mode",
    "work_modes",
    "salary_min",
    "salary_max",
    "salary_bands",
}

STILL_UNWIRED = [c for c in NEW_COLUMNS if c not in WIRED_UP]

# Every module that loads the filter, decides a match, writes a job, or creates a
# preferences row. If a Phase-3 column is ever going to change behaviour, it does
# so by appearing in one of these — which is the whole reason the list is
# enumerated here rather than globbed: a new decider added later is a file
# somebody has to consciously add, not one a glob quietly starts or stops
# covering.
DECIDING_FILES = [
    "notify/match.py",
    "notify/store.py",
    "crawler/enrich.py",
    "crawler/store.py",
    "dashboard/src/lib/match.ts",
    "dashboard/src/lib/preferences.ts",
    "dashboard/src/lib/labels.ts",
    "dashboard/src/lib/jobs.ts",
    "dashboard/src/lib/badges.ts",
    "dashboard/src/lib/link-user.ts",
]


@pytest.mark.parametrize("column, sql_type, default", PREFERENCE_COLUMNS)
def test_schema_adds_the_preferences_column(column: str, sql_type: str, default: str | None) -> None:
    """db/schema.sql declares it, idempotently, with the specced default.

    The default is asserted rather than only the column, because the defaults are
    what make the migration safe: they are what backfills the rows already there.
    A column that arrived NOT NULL without one would fail the ALTER outright; a
    TEXT[] that arrived nullable would hand every later reader a NULL to remember
    to cope with.
    """
    pattern = re.compile(
        rf"ALTER TABLE preferences ADD COLUMN IF NOT EXISTS\s+{re.escape(column)}\s+"
        rf"{re.escape(sql_type)}"
        + (rf"\s+NOT NULL DEFAULT {re.escape(default)}" if default else r"\s*;"),
    )
    assert pattern.search(SCHEMA), (
        f"db/schema.sql does not add preferences.{column} as {sql_type}"
        + (f" NOT NULL DEFAULT {default}" if default else " (nullable, no default)")
    )


@pytest.mark.parametrize("column, sql_type", JOB_COLUMNS)
def test_schema_adds_the_jobs_column(column: str, sql_type: str) -> None:
    """Nullable, no default — so the ALTER is metadata-only and touches no row."""
    pattern = re.compile(
        rf"ALTER TABLE jobs ADD COLUMN IF NOT EXISTS\s+{re.escape(column)}\s+{re.escape(sql_type)}\s*;"
    )
    assert pattern.search(SCHEMA), (
        f"db/schema.sql does not add jobs.{column} as a nullable {sql_type} with no default"
    )


def _executable_text(path: Path) -> str:
    """One source file with its comments and string literals removed.

    A GENUINE STRIP RATHER THAN A LINE-PREFIX HEURISTIC, and the first draft of this
    was the heuristic: it looked for lines not starting with a hash, a double slash
    or a triple quote, and it failed immediately — on the CONTINUATION lines of a
    Python docstring, which start with an ordinary word. (It also could not be
    written down without breaking itself: spelling a triple quote inside a docstring
    ends the docstring, which is the second thing that went wrong here.) The rule
    being asserted is about what the module
    DOES, and every module here explains what it deliberately does not do, at
    length, naming the column while it does so. notify/match.Preferences' own
    docstring says "years_of_experience_held is NOT here, and it is not an
    oversight" — the clearest possible statement of the guarantee, and a
    prose-blind test reads it as the breach.

    Python goes through `tokenize`, which knows exactly which bytes are a COMMENT or
    a STRING (docstrings included, being expression statements). TypeScript gets a
    regex for `/* */` and `//`, which is enough here because these files carry no
    string literal that could contain the name.
    """
    source = path.read_text(encoding="utf-8")

    if path.suffix == ".py":
        kept: list[str] = []
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            kept.append(token.string)
        return "\n".join(kept)

    source = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", "", source)


def _step_zero_statements() -> str:
    """The Phase 3 Step 0 block with its comment lines stripped.

    The block explains itself at length and its prose names plenty of columns it
    does not touch — min_experience, cities, notified. A test that read the
    comments would be asserting something about the English, so it reads what
    actually executes.
    """
    parts = SCHEMA.split("PHASE 3 STEP 0", 1)
    assert len(parts) == 2, "the Phase 3 Step 0 block is missing from db/schema.sql"
    # Drop the remainder of the header's own line — the split lands mid-comment,
    # so what follows the marker is prose that no longer starts with its `--`.
    _, _, body = parts[1].partition("\n")
    # BOUNDED AT THE NEXT SECTION, which it did not need to be while Step 0 was
    # the last thing in the file. Phase 3 Slice 1 appended a block that genuinely
    # does CREATE TABLE, and an unbounded read would attribute it to Step 0 and
    # fail the assertion below for something Step 0 did not do.
    body = body.split("PHASE 3 SLICE 1", 1)[0]
    return "\n".join(line for line in body.splitlines() if not line.lstrip().startswith("--"))


def test_min_experience_is_untouched() -> None:
    """The one experience column that already decides is not re-declared here.

    Phase 3 Step 0 adds storage next to min_experience and must not restate it:
    a second ADD COLUMN for it would be harmless today and would be the line
    somebody edits when they mean to change one of the new ones.
    """
    assert "min_experience" not in _step_zero_statements(), (
        "the Phase 3 Step 0 block names min_experience in a statement rather than "
        "only in its prose"
    )


def test_step_zero_creates_no_table_and_no_index() -> None:
    """No new object, which is why the RLS block above it needed no edit.

    Lock 1 and lock 2 in db/schema.sql enumerate their tables and sequences, so a
    table created after them arrives outside both. This step creates none — and
    that is asserted rather than left as a claim in a comment, because the RLS
    block's own prose predicts Phase 3 as the phase that would create one.
    """
    statements = _step_zero_statements().upper()
    for forbidden in ("CREATE TABLE", "CREATE SEQUENCE", "CREATE INDEX", "CREATE UNIQUE INDEX"):
        assert forbidden not in statements, (
            f"the Phase 3 Step 0 block runs a {forbidden}, so the RLS block above it "
            f"no longer covers everything this file creates — see tests/test_rls.py"
        )


@pytest.mark.parametrize("relative_path", DECIDING_FILES)
def test_no_decider_names_a_still_unwired_column(relative_path: str) -> None:
    """The load-bearing assertion: nothing that decides anything reads these yet.

    Word-boundary matching, not substring, for the reason notify/match.py spends
    a docstring on: `work_mode` is inside `work_modes`, so a substring search
    would report the preferences column as a hit on the jobs one and the failure
    would name the wrong column.

    Eleven columns rather than twelve since Phase 3 Slice 1 — see WIRED_UP.
    """
    source = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
    found = [c for c in STILL_UNWIRED if re.search(rf"\b{re.escape(c)}\b", source)]
    assert not found, (
        f"{relative_path} names Phase-3 Step-0 column(s) {', '.join(found)}. Step 0 "
        f"creates the columns and nothing else — wiring one in is its own feature step, "
        f"with its own migration proof, and it adds the name to WIRED_UP here."
    )



def test_the_crawl_may_write_none_of_the_description_derived_columns() -> None:
    """The hourly crawl cannot stamp an arrangement or a wage. Only a read may.

    Same shape as tests/test_applied_column.py, and the same argument: ownership
    of a column is a property of the SQL that writes the table, not a convention
    the write path has to remember.

    THIS TEST USED TO ASSERT THAT NEITHER STATEMENT NAMED THESE COLUMNS, which was
    the right assertion while no parser existed — the honest value of all three was
    NULL, and these two statements were the only things that could have made it
    otherwise. The work-mode slice took work_mode out of that list, and the salary
    slice takes the pair, because SAVE_DESCRIPTION_FACTS_SQL now writes all three
    on purpose.

    WHAT SURVIVES IS THE HALF THAT MATTERS AND IT NOW COVERS ALL THREE. The
    remaining question is not "can anything write these" but "can the CRAWL write
    these", and the answer must stay no forever, for a reason that gets stronger
    as more columns join: crawler.store.UPSERT_SEEN runs on every re-sighting of
    every row, every hour, and it already rewrites `city` from the location string.
    A crawl that also wrote pay or arrangement would overwrite a
    description-derived verdict with a guess from a board payload — so a job read
    as 18 LPA on Monday would be un-priced by Tuesday, and the digest would change
    for a reason nothing records.
    """
    derived = [c for c, _ in JOB_COLUMNS]
    assert derived == ["work_mode", "salary_min", "salary_max"]

    for column in [*derived, "salary_checked"]:
        assert not re.search(rf"\b{re.escape(column)}\b", UPSERT_SEEN), (
            f"crawler.store.UPSERT_SEEN names jobs.{column}. The crawl has no "
            f"evidence about it and re-runs hourly — writing it there would "
            f"overwrite every verdict the description reader established."
        )


def test_only_the_enrichment_statement_may_write_the_salary_trio() -> None:
    """The salary pair AND salary_checked are written in exactly one statement.

    The sibling of test_only_the_enrichment_statement_may_write_work_mode below,
    and it carries one assertion that function does not need: salary_checked must
    be written as a BOUND PARAMETER rather than as a literal TRUE.

    That is not style. desc_checked is `= TRUE` in the same statement and is
    correct that way, because it means "we asked", which is true even when the
    fetch came back with nothing. salary_checked means "a parser saw a
    description", which a failed fetch is precisely not. Written as a literal it
    would mark every desc_fetch_failed row as parsed-and-empty — re-fusing the two
    facts db/schema.sql split this column out to keep apart, in the one statement
    that could do it.
    """
    for column in ("salary_min", "salary_max", "salary_checked"):
        assert re.search(rf"\b{column}\s*=\s*\$\d", SAVE_DESCRIPTION_FACTS_SQL), (
            f"notify.store.SAVE_DESCRIPTION_FACTS_SQL does not write jobs.{column} "
            f"as a bound parameter. The enrichment rail is the only place with the "
            f"evidence, and salary_checked in particular must not be a literal — a "
            f"failed fetch would then claim a parser had read the posting."
        )


def test_only_the_enrichment_statement_may_write_work_mode() -> None:
    """The CRAWL may not stamp a work arrangement. Only a description read may.

    THIS IS THE GUARANTEE test_the_job_write_paths_... GAVE UP FOR work_mode, and
    it is the sharper half. crawler.store.UPSERT_SEEN runs on every re-sighting of
    every job, every hour, and it already rewrites `city` from the location string.
    The tempting symmetry — "label the arrangement from the location too" — is
    exactly what must not happen here, for two reasons that compound:

      * it has no evidence. A location of "Bangalore, India" says nothing about
        whether the role is remote, and "Remote - US" is a place, not a policy.
      * it would OVERWRITE the description-derived verdict every hour, so a job
        correctly read as remote on Monday would be silently un-remoted by
        Tuesday's crawl, and the digest would change for a reason nothing records.

    So work_mode is written in exactly one statement, on the rail where the
    evidence is, and that is asserted against the statement text rather than left
    as a convention.
    """
    assert not re.search(r"\bwork_mode\b", UPSERT_SEEN), (
        "crawler.store.UPSERT_SEEN names jobs.work_mode. The crawl has no evidence "
        "about a work arrangement and re-runs hourly — writing it there would "
        "overwrite every verdict the description reader established."
    )
    assert re.search(r"\bwork_mode\s*=\s*\$\d", SAVE_DESCRIPTION_FACTS_SQL), (
        "notify.store.SAVE_DESCRIPTION_FACTS_SQL no longer writes jobs.work_mode. "
        "It is the only statement that may."
    )


def test_the_filter_queries_name_none_of_the_still_unwired_columns() -> None:
    """The two statements that decide what reaches an inbox, checked directly.

    Redundant with the file sweep above and deliberately kept: the sweep proves a
    file is clean, this proves the two specific statements are — and they are the
    ones whose text would have to change for a digest to come out different.

    STILL_UNWIRED RATHER THAN EVERY COLUMN, since Slice 2. PREFERENCES_SQL now
    names the experience columns, the city columns and work_modes on purpose, so
    iterating the full list would fail on the very wiring each slice existed to
    do. `salary_bands` is the last preference column still forbidden here, which
    is the half of the guarantee that has not expired.
    """
    for statement_name, statement in (
        ("notify.store.PREFERENCES_SQL", PREFERENCES_SQL),
        ("notify.store.CANDIDATES_SQL", CANDIDATES_SQL),
    ):
        for column in STILL_UNWIRED:
            assert not re.search(rf"\b{re.escape(column)}\b", statement), (
                f"{statement_name} names preferences.{column}"
            )


def test_the_queue_still_names_no_experience_column_at_all() -> None:
    """CANDIDATES_SQL is exempt from the wiring, and that is not an oversight.

    The queue answers "is this job eligible for this person" — open, watched, not
    back catalogue, not already sent. Whether the job is INTERESTING is
    notify/match.py's question, applied in Python over the rows this returns, and
    the experience rule is squarely the second kind. Pushing it into the queue
    would put the same rule in a third place (it is already in Python and, for the
    dashboard's prefilter, in SQL) with no check comparing them.

    So the three wired columns are asserted ABSENT here even though they are
    present in PREFERENCES_SQL, which the sweep above can no longer say for them.
    """
    for column in ("exp_range_low", "exp_range_high", "show_no_experience_jobs"):
        assert not re.search(rf"\b{re.escape(column)}\b", CANDIDATES_SQL), (
            f"notify.store.CANDIDATES_SQL names preferences.{column}. The queue decides "
            f"ELIGIBILITY; notify/match.py decides interest, and the experience rule is "
            f"the second kind."
        )


def test_the_remote_branch_is_live_and_still_india_only() -> None:
    """The relaxation fires now, and it did not grow a way past the country floor.

    THIS REPLACES test_the_remote_branch_is_built_but_inert, which asserted a fact
    the work-mode slice deliberately ended. Every assertion below was in that test
    and still holds; what is gone is the claim that no stored row could reach the
    branch, because notify/description.py now writes the column.

    What is asserted:

      * the branch WORKS — a row marked remote skips a city gate it would fail.
      * a row nobody has read is NOT remote. NULL, absent, empty, onsite and
        hybrid all fail the equality, so the board's unenriched majority is judged
        exactly as it was before. This is the half that would break if somebody
        made NULL mean "unknown, so relax" — the inclusion bias this project
        applies to descriptions and deliberately does NOT apply to location.
      * the country floor still holds. A remote posting is exempt from the CITY
        gate and never from the country; the two relaxing branches share that line
        in city_ok precisely so it cannot be enforced in one and forgotten in the
        other. This is the assertion that would have caught the 20 foreign remote
        postings the live board would otherwise have admitted.
      * ticking `remote` is required to see one. The relaxation cannot smuggle a
        remote posting past the city gate for an account that did not ask for
        remote work — that falls out of the AND in title_stage_ok rather than
        needing a rule of its own, and this is what says so.
    """
    prefs = Preferences(selected_cities=("blr",), keywords=("backend",))
    elsewhere = {"title": "Backend Engineer", "city": "chennai"}

    assert is_match({**elsewhere, "work_mode": "remote"}, prefs)

    for not_remote in (None, "", "onsite", "hybrid"):
        assert not is_match({**elsewhere, "work_mode": not_remote}, prefs), not_remote
    assert not is_match(elsewhere, prefs), "a row with no work_mode key at all"

    assert not is_match(
        {"title": "Backend Engineer", "city": "non-india", "work_mode": "remote"}, prefs
    )

    onsite_only = Preferences(
        selected_cities=("blr",), keywords=("backend",), work_modes=("onsite",)
    )
    assert not is_match({**elsewhere, "work_mode": "remote"}, onsite_only), (
        "an account that did not tick `remote` was shown a remote posting, and the "
        "city gate was relaxed to do it"
    )


def test_the_informational_column_reaches_no_matcher() -> None:
    """years_of_experience_held may be stored and shown, and never consulted.

    THIS REPLACES THE SWEEP COVERAGE THE WIRED_UP EXEMPTION GIVES UP. The column is
    now named in lib/preferences.ts (the SELECT and the UPDATE) and lib/labels.ts
    (the row type), because a person edits it on the preferences screen — so the
    file sweep above cannot forbid it any more. What must stay true is narrower and
    more important: it is how much experience the person HAS, not what they are
    looking for, and it must never decide whether a job is a match.

    Enforced in four places, three of them structural rather than textual:
    notify.match.Preferences has no such field (asserted below), lib/match.ts's
    Preferences type has none, notify/store.PREFERENCES_SQL does not select it, and
    this test forbids the name outright in every module that computes a verdict.

    notify/store.py IS on the list even though it is a query module, because
    PREFERENCES_SQL is what feeds the matcher: a column selected there could be
    read by Preferences.from_row tomorrow with nothing else changing.
    """
    for relative_path in (
        "notify/match.py",
        "notify/store.py",
        "dashboard/src/lib/match.ts",
        "dashboard/src/lib/jobs.ts",
        "dashboard/src/lib/badges.ts",
    ):
        source = _executable_text(PROJECT_ROOT / relative_path)
        assert not re.search(r"\byears_of_experience_held\b", source), (
            f"{relative_path} names years_of_experience_held in executable code. It is "
            f"informational — how much experience the person HAS — and must never decide "
            f"whether a job is a match. See the docstring above."
        )


def test_the_matcher_value_object_carries_exactly_the_ten_deciding_fields() -> None:
    """The last gate: even a loader that selected a new column could not use it.

    notify.match.Preferences is frozen and is the only thing is_match reads, so this
    list IS the filter. It was four fields through Phase 2, seven since Slice 2,
    nine since cities Slice B, ten since the work-mode slice and eleven since the
    salary slice; a twelfth appearing means a column reached the matcher, whatever
    the SQL says.

    THE EXACT LIST, NOT A COUNT, and the order is asserted too — a count would pass
    if somebody swapped years_of_experience_held in for show_no_experience_jobs,
    which is the one substitution that would be both easy to make and wrong in the
    way that matters.

    `cities` IS STILL HERE AND IS NO LONGER THE GATE. Slice B moved the decision to
    selected_cities + all_india and kept the legacy column as the fallback for a row
    that predates the backfill — see Preferences.effective_cities, which is also
    where the sentence naming the slice that deletes it lives. When that happens
    this list goes to eight, and the deletion will fail here first, which is the
    point of asserting the exact list.
    """
    names = [f.name for f in dataclasses.fields(Preferences)]
    assert names == [
        "cities",
        "selected_cities",
        "all_india",
        "keywords",
        "exclusions",
        "max_level",
        "exp_range_low",
        "exp_range_high",
        "show_no_experience_jobs",
        "work_modes",
        "salary_bands",
    ], f"notify.match.Preferences carries {names}"

    assert "years_of_experience_held" not in names, (
        "years_of_experience_held is on notify.match.Preferences. It is informational "
        "— how much experience the person HAS — and putting it where is_match can "
        "reach it makes 'it never filters' a promise instead of a property."
    )


async def test_a_row_with_the_new_columns_loads_and_matches_identically(conn) -> None:
    """Behavioural, against the mirror: the defaults change no answer.

    conftest's set_preferences names the same seven columns it always did, so the
    row this writes takes the migration's DEFAULTs for all nine new ones — which
    is exactly the state the live row is in between the ALTER and the backfill.
    A job that matched before the migration matches in it.
    """
    await conn.set_preferences()
    prefs = await load_preferences(conn, TEST_USER_ID)

    assert prefs == Preferences(
        cities=prefs.cities,
        keywords=prefs.keywords,
        exclusions=prefs.exclusions,
        max_level=prefs.max_level,
    )
    assert is_match({"city": "blr", "title": "Backend Engineer"}, prefs)
    assert not is_match({"city": "other", "title": "Backend Engineer"}, prefs)


def test_the_wired_column_has_exactly_one_writer() -> None:
    """onboarding_completed is written in one statement, and it is an UPDATE.

    The exemption above removes a column from the sweep, so this replaces the
    guarantee rather than merely dropping it. Two things are asserted, and they
    fail for different reasons.

    ONE WRITER: preferences.savePreferences is it. A second place setting this
    would be a second answer to "has this person configured their filter", and
    they would disagree the first time one of them changed.

    NOT ON THE INSERT: lib/link-user.ts creates a new account's preferences row,
    and that row must arrive with onboarding_completed at its schema default of
    FALSE. A signup that marked itself onboarded would make the flag mean "an
    account exists", which is a fact the users table already carries.
    """
    src = PROJECT_ROOT / "dashboard" / "src"
    writers = [
        path.relative_to(PROJECT_ROOT).as_posix()
        for path in sorted(src.rglob("*.ts"))
        if re.search(r"onboarding_completed\s*=\s*TRUE", path.read_text(encoding="utf-8"))
    ]
    assert writers == ["dashboard/src/lib/preferences.ts"], (
        f"onboarding_completed is set to TRUE in {writers or 'nowhere'}; it must be "
        f"written by lib/preferences.savePreferences and by nothing else"
    )

    link_user = (src / "lib" / "link-user.ts").read_text(encoding="utf-8")
    assert "onboarding_completed" not in link_user, (
        "lib/link-user.ts names onboarding_completed. A brand-new account's row must "
        "take the schema default of FALSE — marking it TRUE at signup would make the "
        "column mean 'this account exists' rather than 'this person chose a filter'."
    )
