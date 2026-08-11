"""The dashboard's watch-list write is the Python one, ported — not a second design.

WHAT THIS GUARDS
----------------
`user_companies` had one writer for two phases: db/watchlist.track_companies,
reached from scripts/track_companies.py. That function is a function rather than
an INSERT for one reason, stated at length in its own docstring — creating a
tracking row has TWO halves. The pair says "send me this company's jobs"; the
suppression says "and not the ones it already has", because the digest queue is
"open, in a company you watch, not back catalogue, not yet sent to you" and the
instant the pair exists every stored job of that company is in that person's
queue. jobs.seeded does not cover it: that stops a company's FIRST-EVER crawl
reaching anyone and says nothing about the weeks of ordinary crawling after it.

The picker adds a SECOND writer, in TypeScript, in a runtime that cannot call the
first one — Vercel has no Python and there is no API between the two (CLAUDE.md,
"Two runtimes, one database"). So the statements exist twice, which is a promise
to drift, and this file is the same arrangement tests/test_city_vocabulary.py has
with the city labels and `npm run match-parity` has with the matcher: the
duplication is a fact of the architecture, and a test is what stops it becoming a
lie.

WHY A PYTHON TEST ASSERTS ABOUT TYPESCRIPT — the argument
tests/test_tracked_write_sealed.py makes, unchanged: pytest is the only thing
that runs on every commit. `npm run watchlist-check` proves the behaviour against
the live database and needs Supabase to be reachable; an invariant that holds only
when the database is up is an invariant nobody checks on the commit that breaks
it.

WHAT IT DELIBERATELY DOES NOT ASSERT: that the TypeScript is byte-identical to
the Python. It is not — placeholders differ ($1 versus a tagged template), the TS
carries a per-plan cap the Python has no reason to know about, and it acts on one
company where the Python loops. What is compared is the two STATEMENTS, which is
where a divergence would silently change what an inbox receives.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from db.notifications import SUPPRESS_COMPANY_HISTORY_SQL
from db.watchlist import TRACK_ONE_COMPANY_SQL

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD = PROJECT_ROOT / "dashboard"

# The one file allowed to write this table from the web app, and the only one.
# Same shape as tests/test_tracked_write_sealed.ADD_PATH_FILE: an exemption that
# is a single named file, re-earned below by a test that reads it apart.
WATCHLIST_MODULE = "src/lib/watchlist.ts"


def read(relative: str) -> str:
    return (DASHBOARD / relative).read_text(encoding="utf-8")


def strip_comments(source: str) -> str:
    """Remove /* … */ and // … so prose about a statement is not a statement.

    Load-bearing here for the same reason it is in test_tracked_write_sealed.py:
    lib/watchlist.ts quotes the Python statements it ports in its comments, and a
    rule that forced the documentation to avoid naming its subject would make the
    code worse. `://` is protected so a URL is not read as a comment.
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


# --- the two statements, against the Python originals ----------------------


def test_the_pair_insert_is_the_python_statement(watchlist_ts: str):
    """db/watchlist.TRACK_ONE_COMPANY_SQL, ported.

    ON CONFLICT (user_id, company_id) DO NOTHING is what makes watching
    idempotent, and it is the table's UNIQUE doing the work rather than a check in
    either language. RETURNING company_id is what tells "I have just started
    watching this" from "I already was" — the distinction the suppression hangs
    off, and the reason a DO UPDATE here would be a bug rather than a nicety.
    """
    ported = statement_containing(watchlist_ts, "INSERT INTO user_companies")
    assert normalise(ported) == normalise(TRACK_ONE_COMPANY_SQL), (
        "the dashboard's tracking INSERT has drifted from db/watchlist."
        "TRACK_ONE_COMPANY_SQL.\n"
        f"  python: {normalise(TRACK_ONE_COMPANY_SQL)}\n"
        f"  ts    : {normalise(ported)}"
    )


def test_the_suppression_is_the_python_statement(watchlist_ts: str):
    """db/notifications.SUPPRESS_COMPANY_HISTORY_SQL, ported.

    The half that stops a first digest being an archive dump. INSERT ... SELECT
    rather than a loop, so the set is decided by the database at the instant the
    statement runs; ON CONFLICT DO NOTHING so an untrack/re-track cycle is a
    no-op; RETURNING canonical_id so the caller can report how many rows it
    actually suppressed, which is the number the picker prints.
    """
    ported = statement_containing(watchlist_ts, "INSERT INTO user_job_notifications")
    assert normalise(ported) == normalise(SUPPRESS_COMPANY_HISTORY_SQL), (
        "the dashboard's history suppression has drifted from db/notifications."
        "SUPPRESS_COMPANY_HISTORY_SQL.\n"
        f"  python: {normalise(SUPPRESS_COMPANY_HISTORY_SQL)}\n"
        f"  ts    : {normalise(ported)}"
    )


def test_the_suppression_covers_closed_jobs_too(watchlist_ts: str):
    """EVERY job of that company, not only the open ones.

    A closed job that reopens gets status='open' and miss_count=0 back from
    crawler/store.UPSERT_SEEN while keeping its canonical_id, so scoping this to
    open rows leaves a hole exactly one flap wide — and `reappeared` is precisely
    the set the crawler refuses to treat as news. Asserted on both sides, because
    "the port matches" would still pass if somebody narrowed both.
    """
    ported = statement_containing(watchlist_ts, "INSERT INTO user_job_notifications")
    assert "status" not in ported, (
        "the ported suppression filters on jobs.status. It must not: a closed job "
        "that reopens keeps its identity, so an open-only suppression would let a "
        "flap deliver history as news."
    )
    assert "status" not in SUPPRESS_COMPANY_HISTORY_SQL


# --- the obligation: one transaction, and only on a genuine insert ---------


def watch_company_body(source: str) -> str:
    """The body of lib/watchlist.watchCompany, which is where both halves live."""
    match = re.search(
        r"export async function watchCompany\b.*?\n\}\n", source, flags=re.S
    )
    assert match, "watchCompany has been renamed or restructured — check these assertions"
    return match.group(0)


def test_both_halves_run_in_one_transaction(watchlist_ts: str):
    """No instant in which the pair exists and the suppression does not.

    Not tidiness: the hourly digest runs in exactly that instant if the process
    dies between the two statements, and what it would send is the company's whole
    stored history. db/watchlist.track_companies opens a transaction around the
    same pair of statements for the same reason.
    """
    body = watch_company_body(watchlist_ts)

    assert "sql.begin(" in body, (
        "watchCompany no longer opens a transaction. The pair and the history "
        "suppression must commit together or not at all."
    )
    assert "INSERT INTO user_companies" in body
    assert "INSERT INTO user_job_notifications" in body

    # Both statements must be inside the begin() callback rather than beside it.
    begin_at = body.index("sql.begin(")
    assert body.index("INSERT INTO user_companies") > begin_at
    assert body.index("INSERT INTO user_job_notifications") > begin_at

    # And they must run on the transaction handle, not on the pool. A statement
    # bound to `sql` inside a `sql.begin` callback runs on a DIFFERENT connection
    # and is therefore outside the transaction entirely — the one mistake in this
    # shape that looks completely correct.
    for statement in ("INSERT INTO user_companies", "INSERT INTO user_job_notifications"):
        prefix = body[: body.index(statement)]
        opener = prefix.rindex("`")
        head = prefix[max(0, opener - 120) : opener]
        assert re.search(r"\btx\s*[<`]", head), (
            f"the statement holding {statement!r} does not run on the transaction "
            f"handle `tx`. Bound to the pool it would run on another connection, "
            f"outside the transaction, and the atomicity would be decorative."
        )


def test_the_suppression_runs_only_on_a_genuine_insert(watchlist_ts: str):
    """The sharp edge, ported.

    Suppression records every job the company has RIGHT NOW as already-notified.
    Run against a pair that already exists it would swallow everything that
    arrived since — somebody's actual queue, silently. So it has to sit AFTER the
    branch that returns when the INSERT returned no row, which is what makes
    re-adding genuinely idempotent rather than idempotent-looking.
    """
    body = watch_company_body(watchlist_ts)

    guard = re.search(r"if \(inserted\.length === 0\)", body)
    assert guard, (
        "the RETURNING check is gone from watchCompany. Without it the suppression "
        "runs on every add, and the second click swallows every job that arrived "
        "since the first."
    )
    assert body.index("INSERT INTO user_job_notifications") > guard.start(), (
        "the history suppression runs BEFORE the already-watching check. It must "
        "run only on a genuine insert — see db/watchlist.track_companies."
    )
    # The guard has to leave, not merely log.
    tail = body[guard.start() : body.index("INSERT INTO user_job_notifications")]
    assert "return" in tail, (
        "the already-watching branch does not return, so it falls through into the "
        "suppression it exists to skip."
    )


# --- the seal: one writer, and it is that file -----------------------------

# Every shape a write to the watch list can take. Reads (`FROM user_companies`,
# the EXISTS in lib/companies and the count in lib/account) are deliberately not
# here: the rule is one WRITER, not one reader.
FORBIDDEN_WRITES = (
    r"INSERT\s+INTO\s+user_companies\b",
    r"DELETE\s+FROM\s+user_companies\b",
    r"UPDATE\s+user_companies\b",
    r"INSERT\s+INTO\s+user_job_notifications\b",
    r"DELETE\s+FROM\s+user_job_notifications\b",
    r"UPDATE\s+user_job_notifications\b",
)


def dashboard_sources() -> list[Path]:
    """Every TypeScript file in the deployed app.

    src/ ONLY, unlike tests/test_tracked_write_sealed.py, and the difference is
    the rule rather than an oversight. That file's rule is "this column is not
    the web app's to write, by anyone, ever", so it sweeps the check scripts too.
    This one's rule is "a REQUEST may create a tracking row through exactly one
    function", and a check script is not a request: it is run from a terminal by
    whoever already holds the DSN, against rows it created itself. The two that
    do write these tables are named and re-earned by the test below, which is
    what stops "not swept" quietly becoming "not looked at".
    """
    files = sorted(DASHBOARD.glob("src/**/*.ts"))
    files += sorted(DASHBOARD.glob("src/**/*.tsx"))
    return files


def relative(path: Path) -> str:
    return path.relative_to(DASHBOARD).as_posix()


@pytest.mark.parametrize("path", dashboard_sources(), ids=relative)
def test_only_one_module_writes_the_watch_list(path: Path):
    """One writer, so there is one place the second obligation can be forgotten.

    This is db/watchlist.py's own argument, carried across the language boundary:
    the bulk INSERT that tracked the whole catalog in Phase 1c was DELETED rather
    than kept-but-unused, because a convenient statement that skips the
    suppression is a loaded gun aimed at whichever future caller reaches for it.
    A second TypeScript file writing either table would be that gun, rebuilt.
    """
    if relative(path) == WATCHLIST_MODULE:
        pytest.skip(f"{WATCHLIST_MODULE} is the one writer — the tests above pin what it writes")

    code = strip_comments(path.read_text(encoding="utf-8"))
    for pattern in FORBIDDEN_WRITES:
        match = re.search(pattern, code)
        assert match is None, (
            f"{relative(path)} writes the per-user watch list or notification history "
            f"({match.group(0)!r}). Both tables are written from lib/watchlist.ts and "
            f"nowhere else, because creating a tracking row has a second obligation — "
            f"recording that company's stored jobs as already-seen — and a statement "
            f"that skips it delivers somebody an archive dump."
        )


def test_the_check_scripts_that_write_these_tables_write_only_their_own_rows():
    """The two exemptions, enumerated and re-earned rather than granted.

    Neither is reachable by a request — they are headless scripts run from a
    terminal — but both fabricate per-user rows, and a fabricated row against the
    WRONG user is a person's watch list or notification history edited by a check.
    So each is held to the property that makes it safe:

      badge-check   writes inside a transaction whose only exit is a throw, so it
                    can only ever end in ROLLBACK. Nothing it inserts survives.
      delete-check  writes against an account it created moments earlier, by an id
                    it generated, and deletes it afterwards. A rollback would prove
                    the wrong thing there: the claim under test is that
                    lib/account.deleteAccount really removes rows.
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
        statement = re.search(
            rf"INSERT INTO {table}[^`]*", delete, flags=re.S
        )
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


# --- the cap ---------------------------------------------------------------


def test_the_free_cap_is_the_number_on_the_pricing_card():
    """The landing page promises a limit; this is the same number, enforceable.

    "Watch up to 5 companies" has been on the Free tier since the pricing section
    shipped and was enforced NOWHERE — there was no writer for the table at all,
    so the sentence was marketing copy rather than a rule. The two now have to
    agree, and this is what says so: a cap raised in code and not on the card
    (or the reverse) is a promise the product quietly stops keeping.
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
def test_home_and_jobs_list_the_watch_list_rather_than_the_catalog(page: str):
    """The two screens that print a per-user count follow the person.

    Before the picker there was one list and it could not be wrong: nothing could
    write user_companies, so "the catalog" and "your companies" were the same fifty
    rows. They can differ now, and a "for you" count computed over companies you do
    not watch counts jobs that can never reach your inbox — the disagreement
    between Home and the digest that this slice exists to close.
    """
    source = strip_comments(read(page))
    assert "listWatchedCompanyCards" in source, (
        f"{page} no longer lists the watch list. If it went back to listCompanyCards "
        f"it is showing the shared catalog with per-user match counts on it again."
    )
    assert "listCompanyCards(" not in source


def test_finishing_onboarding_requires_a_watch_list():
    """The fifth required answer, and it is the one the queue asks FIRST.

    notify/store.CANDIDATES_SQL begins with `EXISTS (user_companies …)`, so an
    account watching nothing matches nothing however good its keywords are —
    exactly the empty-digest failure the empty-cities and empty-keywords refusals
    guard against. PUT /api/preferences is the only writer of
    onboarding_completed, so this is where "onboarded implies it can match
    something" is made true.
    """
    route = strip_comments(read("src/app/api/preferences/route.ts"))
    assert "watchesAnything" in route, (
        "PUT /api/preferences no longer checks the watch list, so an account can "
        "complete onboarding watching nothing and get an empty digest forever — "
        "which is the state this slice exists to make impossible."
    )
    refusal = re.search(
        r"if \(!\(await watchesAnything\([^)]*\)\)\) \{(.*?)\n  \}", route, flags=re.S
    )
    assert refusal, "the watch-list branch has been restructured — check this assertion"
    assert "badRequest" in refusal.group(1), (
        "the empty-watch-list branch must REFUSE the save, not log it. An ignored "
        "check reads to the caller as a successful onboarding."
    )
