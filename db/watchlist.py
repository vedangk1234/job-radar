"""The per-user watch list: who tracks which company.

READ BY THE FAN-OUT, NEVER BY THE CRAWL
---------------------------------------
The catalog is shared and is crawled exactly once for everybody —
`scripts/crawl.py` selects `FROM companies WHERE tracked = TRUE` and knows
nothing about users. This module is the other half of "crawl once, fan out": it
answers whose digest a company's jobs belong in, which is a question the notify
path asks and the crawl path must never ask.

So nothing on the crawl path imports this module, and that is asserted rather
than promised — tests/test_user_companies.py greps the source of every crawl-path
module for the table name and for this module's name. If a future change needs
"only crawl companies somebody tracks", the thing to change is not this import
list but the design conversation: a company nobody watches today would go dark
and then re-alert its whole back catalogue the day someone adds it, because
`crawl.py` decides seeding from the stored row count.

`companies.tracked` is a different axis and this module does not read it.
`tracked = FALSE` means "excluded from crawls entirely" — a property of the
catalog. A row here means "this person wants alerts for this company" — a
property of a person. They agree today (every catalog row is tracked, and the
backfill tracks every catalog row for the single user), but they answer different
questions and conflating them is how a dashboard toggle would silently stop a
board being crawled for everyone else.

A PAIR IS AN OVERLAY, NOT A SUBSCRIPTION — SINCE THE FILTER-FIRST SLICE
-----------------------------------------------------------------------
It was a subscription for two phases: the fan-out queue opened with
`EXISTS (user_companies …)`, so a row here was what admitted a company's jobs to
somebody's digest, and creating one therefore had a SECOND obligation — recording
that company's stored jobs as already-notified, or the first digest was an archive
dump. Company tracking is unlimited and free now, so the queue reads a person's
FILTER instead and this table decides nothing about an inbox. The second half of
track_companies() came out with the gate; see there for why leaving it in was the
more dangerous option, and for what would have to come back if the watch list ever
became a filter dimension again.

The SQL lives here rather than in the backfill script so the statement the tests
exercise is the statement production runs — the discipline
tests/test_applied_column.py established for the crawler's writes.
"""

from __future__ import annotations

from typing import Any, Iterable

# The catalog as a set of ids. Read before the inserts by the backfill, so that
# what the migration is responsible for is a snapshot rather than a moving
# target — see scripts/backfill_user_companies.py for why that matters.
CATALOG_IDS_SQL = "SELECT id FROM companies ORDER BY id"

# Tracking ONE company for one user — the only way a tracking row is ever
# written, since Phase 1e.
#
# It used to be an `INSERT ... SELECT id FROM companies WHERE TRUE`, one
# statement for the whole catalog, so that the set was decided by the database at
# the instant it ran. That statement is gone rather than kept-but-unused, and
# deliberately: from 1e until the filter-first slice, creating a pair had a SECOND
# obligation — recording the company's stored history as already-notified — and a
# bulk INSERT that quietly skipped it would have been a loaded gun left in the
# module, aimed at whichever future caller reached for the convenient one. The
# obligation has since moved to the filter (see track_companies), so this is now
# simply the one statement that creates a pair. (The lesson that statement carried
# is worth keeping even though it is gone: SQLite cannot parse
# `INSERT ... SELECT ... ON CONFLICT` when the SELECT has no WHERE — it reads the
# `ON` as beginning a join — and documents `WHERE true` as the disambiguator.
# db/notifications.py has two such statements and both have a real predicate
# there.)
#
# ON CONFLICT (user_id, company_id) DO NOTHING is what makes tracking idempotent,
# and it is the table's UNIQUE constraint doing the work rather than a check in
# Python — the same idiom crawler/discover.TRACK_SQL uses to make "add a company"
# safe to re-run. RETURNING company_id therefore yields a row only on a genuine
# insert, which lets track_companies() tell "I have just started watching this"
# from "I already was" — a distinction that decided whether history was suppressed
# until the filter-first slice, and is now what the caller reports.
TRACK_ONE_COMPANY_SQL = """
INSERT INTO user_companies (user_id, company_id)
VALUES ($1, $2)
ON CONFLICT (user_id, company_id) DO NOTHING
RETURNING company_id
"""

TRACKED_COMPANY_IDS_SQL = """
SELECT company_id
  FROM user_companies
 WHERE user_id = $1
 ORDER BY company_id
"""

COUNT_TRACKED_SQL = "SELECT count(*) FROM user_companies WHERE user_id = $1"


async def catalog_company_ids(conn) -> list[int]:
    """Every company id in the shared catalog, ascending.

    No `tracked` filter: this is the catalog, not the crawl's working set.
    """
    return [row["id"] for row in await conn.fetch(CATALOG_IDS_SQL)]


async def tracked_company_ids(conn, user_id: Any) -> list[int]:
    """The company ids this user watches, ascending."""
    return [row["company_id"] for row in await conn.fetch(TRACKED_COMPANY_IDS_SQL, user_id)]


async def count_tracked(conn, user_id: Any) -> int:
    """How many companies this user watches."""
    return await conn.fetchval(COUNT_TRACKED_SQL, user_id)


async def track_companies(conn, user_id: Any, company_ids: Iterable[int]) -> list[int]:
    """Start watching these companies.

    Returns the ids genuinely newly tracked; one already tracked contributes
    nothing, exactly as track_all_companies reports.

    THIS FUNCTION USED TO HAVE A SECOND HALF AND THE FILTER-FIRST SLICE REMOVED
    IT. Until then it wrote the pair AND recorded that company's stored jobs as
    already-notified, in one transaction, because the digest queue opened with
    `EXISTS (user_companies …)` — so the instant a pair existed, every job the
    company already held was in that person's queue and their first digest was an
    archive dump.

    That was correct for as long as tracking a company was how somebody SUBSCRIBED
    to it. It is not any more: the queue no longer reads this table at all, so a
    pair grants nothing and withholding history on the strength of it is not
    belt-and-braces, it is a write aimed at the wrong event. Concretely, and this
    is why it had to come out rather than being left as harmless insurance:

        you save your filter at T0, so everything matching then is suppressed.
        At T1 job J appears at company X, matches, and enters your queue. At T2,
        before the next digest, you pin X on /watchlist — an act that under
        filter-first grants you nothing you did not already have — and the
        suppression records every stored job of X, J included. J leaves your queue
        silently, and leaves behind a row saying the withholding was deliberate.

    A job removed from somebody's queue by an unrelated action, with a receipt
    claiming it was meant, is the worst-shaped failure in this codebase — it is
    the one tests/test_leakage.py's fifth property exists to exclude. So the
    obligation moved rather than being duplicated: it now hangs off the FILTER
    changing, in notify/store.suppress_filter_backlog, which is where the match
    set is now decided.

    IF THE WATCH LIST EVER BECOMES A FILTER DIMENSION AGAIN — an opt-in "only
    email me companies I watch" toggle is the obvious shape — the suppression has
    to come back WITH it, in this function, in one transaction, driven by
    RETURNING so it runs only on a genuine insert. Both halves of that sentence
    are load bearing and the git history has the implementation: suppressing over
    a pair that already exists swallows everything that arrived since, which is
    somebody's actual queue.

    db/notifications.SUPPRESS_COMPANY_HISTORY_SQL went WITH it rather than being
    left in place for that day, and deliberately — this module deleted its own
    bulk TRACK_ALL_COMPANIES_SQL on the identical argument in Phase 1e. A
    statement written by nobody and read by nobody is worse than absent: the next
    person to find it reasonably assumes something depends on it, and here the
    first edit made on that assumption re-aims a suppression at an event that no
    longer grants anything. Retrieving fifteen lines from the log is cheaper than
    that.
    """
    ids = list(company_ids)
    if not ids:
        return []

    tracked: list[int] = []
    async with conn.transaction():
        for company_id in ids:
            rows = await conn.fetch(TRACK_ONE_COMPANY_SQL, user_id, company_id)
            if not rows:
                continue  # already tracked
            tracked.append(rows[0]["company_id"])

    return tracked


async def track_all_companies(conn, user_id: Any) -> list[int]:
    """Track every catalog company for this user. Returns the ids newly inserted.

    An empty list means every company was already tracked, which is the ordinary
    result of a second run — not an error and not a no-op to report as a failure.

    Goes through track_companies rather than restating its INSERT, which is what
    keeps "there is one statement that creates a tracking row" true. That mattered
    more when creating one carried a second obligation; it is still how the bulk
    path and the ordinary path are kept from drifting.

    A company inserted into the catalog between the read below and the inserts is
    simply not tracked by this run. That window is not new and is not an
    oversight: scripts/backfill_user_companies.py gates on coverage of a snapshot
    taken before its insert for exactly this reason — the discovery sweep can add
    a company mid-run, and re-running picks it up.
    """
    return await track_companies(conn, user_id, await catalog_company_ids(conn))
