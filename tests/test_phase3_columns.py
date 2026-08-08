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
WIRED_UP = {
    "onboarding_completed",
    "exp_range_low",
    "exp_range_high",
    "show_no_experience_jobs",
    "years_of_experience_held",
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



def test_the_job_write_paths_name_none_of_the_new_job_columns() -> None:
    """The crawl and enrichment statements cannot fill these in by accident.

    Same shape as tests/test_applied_column.py, and the same argument: ownership
    of a column is a property of the SQL that writes the table, not a convention
    the write path has to remember. Until the parser step exists, the honest
    value of all three is NULL, and these two statements are the only things that
    could make it otherwise.
    """
    for statement_name, statement in (
        ("crawler.store.UPSERT_SEEN", UPSERT_SEEN),
        ("notify.store.SAVE_DESCRIPTION_FACTS_SQL", SAVE_DESCRIPTION_FACTS_SQL),
    ):
        for column, _ in JOB_COLUMNS:
            assert not re.search(rf"\b{re.escape(column)}\b", statement), (
                f"{statement_name} names jobs.{column}, which no statement may write "
                f"until the enrichment parser step"
            )


def test_the_filter_queries_name_none_of_the_still_unwired_columns() -> None:
    """The two statements that decide what reaches an inbox, checked directly.

    Redundant with the file sweep above and deliberately kept: the sweep proves a
    file is clean, this proves the two specific statements are — and they are the
    ones whose text would have to change for a digest to come out different.

    STILL_UNWIRED RATHER THAN EVERY COLUMN, since Slice 2. PREFERENCES_SQL now
    names the three experience columns on purpose, so iterating the full list would
    fail on the very wiring this slice exists to do. The four remaining preference
    columns (selected_cities, all_india, work_modes, salary_bands) are still
    forbidden here, which is the half of the guarantee that has not expired.
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


def test_the_matcher_value_object_carries_exactly_the_seven_deciding_fields() -> None:
    """The last gate: even a loader that selected a new column could not use it.

    notify.match.Preferences is frozen and is the only thing is_match reads, so this
    list IS the filter. It was four fields through Phase 2 and is seven since Slice
    2; an eighth appearing means a column reached the matcher, whatever the SQL says.

    THE EXACT LIST, NOT A COUNT, and the order is asserted too — a count would pass
    if somebody swapped years_of_experience_held in for show_no_experience_jobs,
    which is the one substitution that would be both easy to make and wrong in the
    way that matters.
    """
    names = [f.name for f in dataclasses.fields(Preferences)]
    assert names == [
        "cities",
        "keywords",
        "exclusions",
        "max_level",
        "exp_range_low",
        "exp_range_high",
        "show_no_experience_jobs",
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
