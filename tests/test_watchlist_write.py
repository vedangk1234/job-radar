"""The dashboard's per-user writes: one table each, and the Python they port.

WHAT THIS GUARDS
----------------
Two tables in this app are written per person, and until the filter-first slice
BOTH were written from one module. `lib/watchlist.watchCompany` created the
tracking pair AND recorded that company's stored jobs as already-notified, in one
transaction, because the digest queue opened with `EXISTS (user_companies …)` —
so the instant a pair existed, every job that company already held was in that
person's queue and their first digest was an archive dump.

The queue does not read `user_companies` any more. A pair admits nothing, so
suppressing on the strength of one takes away jobs a person's filter has already
earned them (db/watchlist.track_companies has the sequence). The obligation moved
to the filter, which is where the match set is now decided, and with it the write:

    user_companies          <- lib/watchlist.ts          and nothing else
    user_job_notifications  <- lib/preferences.ts        and nothing else

Two tables, one writer each, which is a tighter seal than the one writer for two
that came before it. FORBIDDEN_WRITES is therefore two parametrised sets rather
than one list with an exemption bolted on: an exemption is a hole somebody widens,
a second set is a rule somebody has to state.

WHY A PYTHON TEST ASSERTS ABOUT TYPESCRIPT — the argument
tests/test_tracked_write_sealed.py makes, unchanged: pytest is the only thing
that runs on every commit. `npm run filter-first-check` proves the behaviour
against the live database and needs Supabase to be reachable; an invariant that
holds only when the database is up is an invariant nobody checks on the commit
that breaks it.

WHAT IT DELIBERATELY DOES NOT ASSERT: that the TypeScript is byte-identical to
the Python. It is not — placeholders differ ($1 versus a tagged template), the TS
carries a per-plan cap the Python has no reason to know about, and it acts on one
company where the Python loops. What is compared is the STATEMENTS, which is
where a divergence would silently change what an inbox receives.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from db.watchlist import TRACK_ONE_COMPANY_SQL

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD = PROJECT_ROOT / "dashboard"

# One module per table, and the pairing is the rule this file exists to hold.
# Same shape as tests/test_tracked_write_sealed.ADD_PATH_FILE: each is a single
# named file, re-earned below by tests that read it apart.
WATCHLIST_MODULE = "src/lib/watchlist.ts"
PREFERENCES_MODULE = "src/lib/preferences.ts"


def read(relative: str) -> str:
    return (DASHBOARD / relative).read_text(encoding="utf-8")


def strip_comments(source: str) -> str:
    """Remove /* … */ and // … so prose about a statement is not a statement.

    Load-bearing here for the same reason it is in test_tracked_write_sealed.py:
    both modules quote the statements they port in their comments, and a rule
    that forced the documentation to avoid naming its subject would make the code
    worse. `://` is protected so a URL is not read as a comment.
    """
    source = re.sub(r"/\*.*?\*/", " ", source, flags=re.S)
    return re.sub(r"(?<!:)//[^\n]*", " ", source)


def normalise(statement: str) -> str:
    """One SQL statement, comparable across two languages.

    Placeholders collapse to `?` — asyncpg writes `$1`, postgres.js interpolates
    `${userId}` — and whitespace collapses to single spaces. Nothing else is
    touched: table names, column lists, the ON CONFLICT target and the RETURNING
    clause all have to match character for character, because each of them is a
    decision the Python's comments spend paragraphs on.
    """
    text = re.sub(r"\$\{[^}]+\}", "?", statement)
    text = re.sub(r"\$\d+", "?", text)
    return re.sub(r"\s+", " ", text).strip()


def template_literals(source: str) -> list[str]:
    """Every `…` template literal in a TypeScript file, in order."""
    return re.findall(r"`([^`]*)`", source, flags=re.S)


def statement_containing(source: str, needle: str) -> str:
    """The one template literal holding `needle`. Fails loudly if it is not unique."""
    found = [literal for literal in template_literals(source) if needle in literal]
    assert found, f"no SQL template literal in the module contains {needle!r}"
    assert len(found) == 1, (
        f"{len(found)} statements contain {needle!r}; this test compares ONE, and two "
        f"statements writing the same table is exactly what db/watchlist.py deleted its "
        f"bulk INSERT to prevent"
    )
    return found[0]


@pytest.fixture(scope="module")
def watchlist_ts() -> str:
    return strip_comments(read(WATCHLIST_MODULE))


@pytest.fixture(scope="module")
def preferences_ts() -> str:
    return strip_comments(read(PREFERENCES_MODULE))


# --- the pair insert, against the Python original --------------------------


def test_the_pair_insert_is_the_python_statement(watchlist_ts: str):
    """db/watchlist.TRACK_ONE_COMPANY_SQL, ported.

    ON CONFLICT (user_id, company_id) DO NOTHING is what makes watching
    idempotent, and it is the table's UNIQUE doing the work rather than a check in
    either language. RETURNING company_id is what tells "I have just started
    watching this" from "I already was" — which decided whether history was
    suppressed until the filter-first slice, and is now what the caller reports.
    """
    ported = statement_containing(watchlist_ts, "INSERT INTO user_companies")
    assert normalise(ported) == normalise(TRACK_ONE_COMPANY_SQL), (
        "the dashboard's tracking INSERT has drifted from db/watchlist."
        "TRACK_ONE_COMPANY_SQL.\n"
        f"  python: {normalise(TRACK_ONE_COMPANY_SQL)}\n"
        f"  ts    : {normalise(ported)}"
    )


def test_watching_a_company_no_longer_writes_notification_rows(watchlist_ts: str):
    """THE DELETION, ASSERTED RATHER THAN ASSUMED — and it is the sharp one.

    lib/watchlist.ts wrote `user_job_notifications` for two slices, and it was
    right to: the queue's first clause was the watch list, so a pair admitted a
    company's whole stored history at once. With the gate gone a pair admits
    nothing, and suppressing on the strength of one is not harmless insurance —
    it silently removes jobs the person's FILTER already put in their queue:

        filter saved at T0, so everything matching then is suppressed. Job J
        appears at company X at T1, matches, enters the queue. At T2 the person
        pins X — which grants them nothing new — and the suppression records every
        stored job of X, J included. J leaves the queue, behind a row saying it
        was meant.

    That is the failure tests/test_leakage.py's fifth property exists to exclude,
    reached from inside rather than from another account. So the statement has to
    be ABSENT here, not merely unreachable.
    """
    assert "user_job_notifications" not in watchlist_ts, (
        "lib/watchlist.ts names user_job_notifications again. Watching a company "
        "must not touch notification state: the queue no longer reads the watch "
        "list, so suppressing a company's history on a pin removes jobs the "
        "person's filter already earned them. See db/watchlist.track_companies."
    )


# --- the suppression, on the filter instead --------------------------------


def preferences_save_body(source: str) -> str:
    """The body of lib/preferences.savePreferences, where the transaction lives."""
    match = re.search(
        r"export async function savePreferences\b.*?\n\}\n", source, flags=re.S
    )
    assert match, "savePreferences has been renamed or restructured — check these assertions"
    return match.group(0)


def suppression_body(source: str) -> str:
    """The body of lib/preferences.suppressFilterBacklog."""
    match = re.search(
        r"async function suppressFilterBacklog\b.*?\n\}\n", source, flags=re.S
    )
    assert match, "suppressFilterBacklog has been renamed or restructured"
    return match.group(0)


def test_the_suppression_insert_shares_every_deciding_clause_with_the_python(
    preferences_ts: str,
):
    """The port, compared on the clauses that decide rather than character for
    character — and the asymmetry is FORCED rather than chosen.

    db/notifications.record_notified_sql builds `VALUES ($1, $2), ($1, $3), …` in
    chunks of 400 because it must stay inside the subset Postgres and SQLite agree
    on: the whole pytest suite runs those statements against the in-memory shim,
    which has no `unnest`. Nothing on the TypeScript side runs against SQLite, and
    one array parameter binds no per-row placeholder, so both the 65535-parameter
    ceiling and the chunking it forces disappear.

    What the two must still share is every clause that changes an outcome: the
    table, the column pair, the ON CONFLICT target and action (so a re-save is a
    genuine no-op rather than an error or a re-stamp), and RETURNING canonical_id
    (so the count reported to the person is rows WRITTEN, not rows offered).
    """
    ported = statement_containing(preferences_ts, "INSERT INTO user_job_notifications")
    flat = normalise(ported)

    assert "INSERT INTO user_job_notifications (user_id, canonical_id)" in flat
    assert "ON CONFLICT (user_id, canonical_id) DO NOTHING" in flat, (
        "the suppression is no longer ON CONFLICT DO NOTHING. Re-saving a filter "
        "would then error or re-stamp rather than being the no-op it has to be."
    )
    assert "RETURNING canonical_id" in flat, (
        "the suppression no longer returns what it wrote, so the count the save "
        "reports to a person would be rows OFFERED rather than rows written."
    )
    assert "DO UPDATE" not in flat


def test_the_suppression_read_is_status_blind(preferences_ts: str):
    """EVERY job the filter admits, not only the open ones.

    A closed job that reopens comes back `status = 'open'` with `miss_count = 0`
    from crawler/store.UPSERT_SEEN while KEEPING its canonical_id. So a suppression
    scoped to open rows leaves a hole exactly one flap wide: the job sits closed and
    unsuppressed while somebody saves their filter, a vendor re-lists it a week
    later, and it arrives in that person's digest as news because a board was
    edited. `reappeared` is precisely the set crawler/diff.py refuses to treat as
    news.

    This is the property db/notifications.SUPPRESS_COMPANY_HISTORY_SQL held before
    it, asserted on the thing that replaced it — the replacement had to re-earn it
    and could easily not have.
    """
    body = suppression_body(preferences_ts)
    read_statement = next(
        literal for literal in template_literals(body) if "FROM jobs j" in literal
    )
    assert "status" not in read_statement, (
        "the suppression read filters on jobs.status. It must not: a closed job "
        "that reopens keeps its identity, so an open-only suppression lets a flap "
        "deliver history as news. See notify/store.SUPPRESSION_CANDIDATES_SQL."
    )
    # And it must still carry the other two, which are what make it the QUEUE's
    # question rather than a different one.
    assert "j.seeded = FALSE" in read_statement, (
        "the suppression read dropped `seeded = FALSE`. A seeded row is excluded "
        "from every queue forever anyway, so recording one costs a row per user "
        "per seeded job — 6,496 from a single sweep."
    )
    assert "user_job_notifications" in read_statement


def test_the_save_and_the_suppression_run_in_one_transaction(preferences_ts: str):
    """No instant in which the filter is widened and the suppression is not written.

    Not tidiness: the hourly digest runs in exactly that instant if the process
    dies between the two, and what it would send is every stored job the new answer
    just admitted. lib/watchlist.watchCompany held a transaction around the same
    shape of obligation for the same reason, until the obligation moved here.
    """
    body = preferences_save_body(preferences_ts)

    assert "sql.begin(" in body, (
        "savePreferences no longer opens a transaction. The UPDATE and the "
        "backlog suppression must commit together or not at all."
    )
    begin_at = body.index("sql.begin(")
    assert body.index("UPDATE preferences") > begin_at
    assert body.index("suppressFilterBacklog") > begin_at, (
        "the suppression is called outside the transaction the UPDATE runs in."
    )


def test_every_statement_of_the_save_runs_on_the_transaction_handle(preferences_ts: str):
    """`tx`, never `sql` — the one mistake in this shape that looks correct.

    A statement bound to the pool inside a `sql.begin` callback runs on a DIFFERENT
    connection and is therefore outside the transaction entirely, so the atomicity
    above would be decorative. Asserted textually because nothing else can see it:
    the code compiles, runs, and produces the right answer on every run that does
    not crash in the window.
    """
    for source in (preferences_save_body(preferences_ts), suppression_body(preferences_ts)):
        for statement in ("UPDATE preferences", "INSERT INTO user_job_notifications", "FROM jobs j"):
            if statement not in source:
                continue
            prefix = source[: source.index(statement)]
            opener = prefix.rindex("`")

            # THE HANDLE IS FOUND BY WALKING BACK TO THE `await` RATHER THAN BY
            # SEARCHING A FIXED WINDOW, and that is not a refinement — the first
            # draft of this test used `prefix[opener - 160 : opener]` and FAILED on
            # a statement that was perfectly correct. The suppression read carries
            # a ~400-character inline row type between `tx<` and its backtick, so a
            # window wide enough for that statement is wide enough to catch an
            # unrelated `tx` several lines above a statement bound to the pool —
            # which is the failure this test exists to detect. Anchoring on the
            # awaited expression is exact at any length.
            awaited = re.findall(r"await\s+([A-Za-z_$][\w$]*)", prefix)
            assert awaited, f"no awaited call precedes {statement!r}"
            assert awaited[-1] == "tx", (
                f"the statement holding {statement!r} runs on `{awaited[-1]}` rather "
                f"than on the transaction handle `tx`. Bound to the pool it would run "
                f"on another connection, outside the transaction, and the atomicity "
                f"would be decorative."
            )


def test_the_suppression_is_a_delta_rather_than_everything_matching(preferences_ts: str):
    """NEW \\ OLD, and the subtraction is the whole design.

    "Suppress everything currently matching on every save" would be simpler and is
    wrong: it eats whatever arrived since the last digest EVERY TIME anybody
    touches the form, including a save that narrows the filter or edits a field the
    matcher never reads. Only what the new answer newly admits is history.

    Day one is not a special case in that rule — a brand-new row is blank on every
    field and every blank in this app is fail-closed, so OLD is empty and the first
    save records the whole current match set. Asserted on the shape of the
    expression, because the alternative reads almost identically.
    """
    body = suppression_body(preferences_ts)
    assert re.search(r"isMatch\([^)]*,\s*after\)\s*&&\s*!isMatch\([^)]*,\s*before\)", body), (
        "the suppression no longer computes NEW \\ OLD. Suppressing everything the "
        "new filter matches would silently swallow whatever arrived since the last "
        "digest, on every save, including one that narrows the filter."
    )


def test_the_suppression_judges_with_the_real_matcher(preferences_ts: str):
    """isMatch, not the SQL prefilter's shape.

    lib/jobs.ts's clauses are allowed to be WIDER than isMatch — that is stated
    three times in that file and is what keeps the prefilter safe. Using their
    shape here would suppress rows that never matched, and those are exactly the
    rows enrichment can later turn into genuine matches by filling a NULL
    min_experience the filter then admits. Over-suppressing today is swallowing a
    real match tomorrow.
    """
    body = suppression_body(preferences_ts)
    assert "isMatch(" in body, (
        "the backlog suppression no longer runs isMatch. A prefilter-shaped "
        "approximation suppresses rows that never matched."
    )


# --- the seal: two tables, one writer each ---------------------------------

# Every shape a write can take, per table. Reads are deliberately not here — the
# rule is one WRITER, not one reader, so lib/companies' EXISTS and lib/account's
# counts are unaffected.
WRITE_SHAPES = ("INSERT\\s+INTO\\s+{table}\\b", "DELETE\\s+FROM\\s+{table}\\b", "UPDATE\\s+{table}\\b")

# The pairing this file exists to hold. It was ONE writer for TWO tables until the
# filter-first slice; splitting it into two parametrised sets rather than adding an
# exemption is deliberate — an exemption is a hole the next person widens, and a
# second set is a rule they have to state.
SEALED_TABLES = {
    "user_companies": WATCHLIST_MODULE,
    "user_job_notifications": PREFERENCES_MODULE,
}


def dashboard_sources() -> list[Path]:
    """Every TypeScript file in the deployed app.

    src/ ONLY, unlike tests/test_tracked_write_sealed.py, and the difference is
    the rule rather than an oversight. That file's rule is "this column is not
    the web app's to write, by anyone, ever", so it sweeps the check scripts too.
    This one's rule is "a REQUEST may write these tables through exactly one
    function each", and a check script is not a request: it is run from a terminal
    by whoever already holds the DSN, against rows it created itself. The three
    that do write these tables are named and re-earned by the test below, which is
    what stops "not swept" quietly becoming "not looked at".
    """
    files = sorted(DASHBOARD.glob("src/**/*.ts"))
    files += sorted(DASHBOARD.glob("src/**/*.tsx"))
    return files


def relative(path: Path) -> str:
    return path.relative_to(DASHBOARD).as_posix()


@pytest.mark.parametrize("table,writer", sorted(SEALED_TABLES.items()))
@pytest.mark.parametrize("path", dashboard_sources(), ids=relative)
def test_each_per_user_table_has_exactly_one_writer(path: Path, table: str, writer: str):
    """One writer per table, so there is one place each obligation can be forgotten.

    This is db/watchlist.py's own argument, carried across the language boundary:
    the bulk INSERT that tracked the whole catalog in Phase 1c was DELETED rather
    than kept-but-unused, because a convenient statement that skips an obligation
    is a loaded gun aimed at whichever future caller reaches for it. A second
    TypeScript file writing either table would be that gun, rebuilt.

    Note that the two writers are now DIFFERENT files, which is what makes this
    parametrised rather than a single sweep: lib/watchlist.ts writing
    user_job_notifications is exactly the regression the filter-first slice
    removed, and this catches it as a violation of the OTHER table's seal.
    """
    if relative(path) == writer:
        pytest.skip(f"{writer} is the one writer of {table} — the tests above pin what it writes")

    code = strip_comments(path.read_text(encoding="utf-8"))
    for shape in WRITE_SHAPES:
        match = re.search(shape.format(table=table), code)
        assert match is None, (
            f"{relative(path)} writes {table} ({match.group(0)!r}). That table is "
            f"written from {writer} and nowhere else, because the write carries an "
            f"obligation — a tracking row is capped per plan, and a notification row "
            f"is what stops somebody being emailed a job. A statement that skips "
            f"either delivers an archive dump or an unenforceable limit."
        )


def test_the_check_scripts_that_write_these_tables_write_only_their_own_rows():
    """The exemptions, enumerated and re-earned rather than granted.

    None is reachable by a request — they are headless scripts run from a
    terminal — but all of them fabricate per-user rows, and a fabricated row
    against the WRONG user is a person's watch list or notification history edited
    by a check. So each is held to the property that makes it safe:

      badge-check        writes inside a transaction whose only exit is a throw, so
                         it can only ever end in ROLLBACK. Nothing it inserts
                         survives.
      delete-check       writes against an account it created moments earlier, by
                         an id it generated, and deletes it afterwards. A rollback
                         would prove the wrong thing there: the claim under test is
                         that lib/account.deleteAccount really removes rows.
      filter-first-check the same shape as delete-check and for the same reason —
                         the claim under test is that a real save against a real
                         account really writes suppression rows, which a rolled-back
                         transaction could not demonstrate.
    """
    badge = strip_comments(read("scripts/badge-check.ts"))
    insert_at = badge.index("INSERT INTO user_job_notifications")
    assert badge.index("sql.begin(") < insert_at, (
        "badge-check's notification INSERT is no longer inside its transaction, so "
        "the rows it fabricates would survive the run."
    )
    assert "throw new Rollback()" in badge, (
        "badge-check no longer forces a rollback. Its second account and that "
        "account's notification row would then be permanent."
    )

    delete = strip_comments(read("scripts/delete-check.ts"))
    for table in ("user_companies", "user_job_notifications"):
        statement = re.search(rf"INSERT INTO {table}[^`]*", delete, flags=re.S)
        assert statement, f"delete-check no longer seeds {table}"
        assert "who.accountId" in statement.group(0), (
            f"delete-check's {table} INSERT no longer binds the throwaway account id "
            f"it generated. Bound to anything else it would be writing rows against a "
            f"real person."
        )
    assert "DELETE FROM public.users" in delete, (
        "delete-check no longer removes the account it created, so its fabricated "
        "rows would accumulate."
    )

    first = strip_comments(read("scripts/filter-first-check.ts"))
    assert "DELETE FROM public.users" in first, (
        "filter-first-check no longer removes the throwaway account it creates, so "
        "the rows its save suppresses would accumulate against a real row."
    )


# --- the cap ---------------------------------------------------------------


def test_the_free_cap_is_the_number_on_the_pricing_card():
    """The landing page promises a limit; this is the same number, enforceable.

    "Watch up to 5 companies" has been on the Free tier since the pricing section
    shipped. The two have to agree, and this is what says so: a cap raised in code
    and not on the card (or the reverse) is a promise the product quietly stops
    keeping.

    UNTOUCHED BY THE FILTER-FIRST SLICE, and worth saying why rather than leaving
    it to look like an oversight: the cap limits how many companies somebody may
    PIN, and pinning is still a real feature with a real cost on the pricing page.
    What changed is that a pin no longer gates a digest — so the cap no longer caps
    what anybody can be emailed about, which it never should have.
    """
    labels = read("src/lib/labels.ts")
    constant = re.search(r"FREE_WATCHLIST_CAP\s*=\s*(\d+)", labels)
    assert constant, "FREE_WATCHLIST_CAP is gone from lib/labels.ts"

    landing = read("src/app/signin/page.tsx")
    promised = re.search(r"Watch up to (\d+) companies", landing)
    assert promised, (
        "the Free tier on the landing page no longer says 'Watch up to N companies'. "
        "If the promise changed, change the constant with it."
    )
    assert constant.group(1) == promised.group(1), (
        f"the pricing card promises {promised.group(1)} companies and "
        f"FREE_WATCHLIST_CAP is {constant.group(1)}."
    )


def watch_company_body(source: str) -> str:
    """The body of lib/watchlist.watchCompany."""
    match = re.search(r"export async function watchCompany\b.*?\n\}\n", source, flags=re.S)
    assert match, "watchCompany has been renamed or restructured — check these assertions"
    return match.group(0)


def test_the_cap_is_enforced_where_the_row_is_written(watchlist_ts: str):
    """Server-side, in the one function that can create a pair — not in the picker.

    The KEYWORD_CAP argument, one table over: a cap that lives in the browser is a
    suggestion to anyone holding curl. It is checked inside the same transaction
    that does the insert, so a refusal rolls back rather than leaving a pair
    behind.
    """
    body = watch_company_body(watchlist_ts)
    assert "watchlistCapForPlan" in body, "the cap is no longer read inside watchCompany"
    assert "WatchlistFullError" in body, "watchCompany no longer refuses over the cap"

    picker = strip_comments(read("src/components/WatchlistPicker.tsx"))
    assert "WatchlistFullError" not in picker, (
        "the picker imports the server's refusal type, which suggests the limit is "
        "being decided in the browser. The screen may DISABLE the button; the rule "
        "lives in lib/watchlist."
    )


# --- what the screens now list ---------------------------------------------


@pytest.mark.parametrize("page", ["src/app/page.tsx", "src/app/jobs/page.tsx"])
def test_home_and_jobs_list_the_catalog_rather_than_the_watch_list(page: str):
    """THE INVERSE OF WHAT THIS ASSERTED BEFORE, and the inversion is the slice.

    The picker slice narrowed both screens to `user_companies`, on the argument
    that a screen printing a per-user "for you" count has to follow the person —
    a count over a company you did not watch counted jobs that could never reach
    your inbox. Sound, and its premise is gone: the digest queue no longer reads
    the watch list, so a match at any company can reach any inbox.

    The count itself never moved. lib/jobs.matchCountsByCompany has always spanned
    the catalog, so the narrowing only ever affected which company ROWS were
    printed beside numbers that already covered everything. This is the page
    agreeing with its own figures again.

    Asserted rather than left to the eye because the failure is quiet in the
    dangerous direction: back on listWatchedCompanyCards, a new account would see
    an empty Home while its digest filled up.
    """
    source = strip_comments(read(page))
    assert "listCompanyCards" in source, (
        f"{page} no longer lists the catalog."
    )
    assert "listWatchedCompanyCards" not in source, (
        f"{page} is back on the watch-list query. Under filter-first that shows a "
        f"person fewer companies than their digest is computed from — an empty Home "
        f"beside a full inbox."
    )


def test_finishing_onboarding_does_not_require_a_watch_list():
    """THE SIXTH REQUIRED ANSWER IS GONE, asserted as an absence.

    PUT /api/preferences refused a save from an account watching nothing, and it
    belonged to the family holding the city answer, the keyword list, the
    experience range and the work arrangement — every one of which guards a row
    that matches NO JOB AT ALL. It earned its place the same way: the queue asked
    `EXISTS (user_companies …)` first, so an account watching nothing matched
    nothing however good its keywords were.

    That failure cannot happen now, so the refusal guarded nothing while still
    requiring an answer that decides nothing — and on a setup screen a required
    question whose answer changes no outcome is worse than no question, because a
    person reasonably concludes it must matter.

    The picker, the cap and /watchlist are all untouched, which is the point: the
    watch list stopped being a GATE, not a feature.
    """
    route = strip_comments(read("src/app/api/preferences/route.ts"))
    assert "watchesAnything" not in route, (
        "PUT /api/preferences checks the watch list again. Under filter-first an "
        "account watching nothing matches the whole catalog, so the refusal blocks "
        "a save for a reason that is no longer true."
    )

    watchlist_source = strip_comments(read(WATCHLIST_MODULE))
    assert "export async function watchesAnything" not in watchlist_source, (
        "watchesAnything is exported again with no caller. A function written for "
        "nobody is the jobs.notified lesson applied to a name."
    )
