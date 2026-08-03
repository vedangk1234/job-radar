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

CREATING A PAIR HAS TWO HALVES, SINCE PHASE 1e
-----------------------------------------------
A tracking row does not only say "send me this company's jobs" — it also decides
what counts as this person's history. The fan-out queue is "open, in a company
you watch, not back catalogue, not yet sent to you", so the moment the row exists
every job that company already has is in that person's queue. jobs.seeded keeps a
company's first-ever crawl out of everyone's digest and says nothing about the
weeks of ordinary crawling after it.

So track_companies() writes the pair and records the company's stored jobs as
already-notified for that user, in one transaction, and it is the only path that
writes this table. See db/notifications.SUPPRESS_COMPANY_HISTORY_SQL.

The SQL lives here rather than in the backfill script so the statement the tests
exercise is the statement production runs — the discipline
tests/test_applied_column.py established for the crawler's writes.
"""

from __future__ import annotations

from typing import Any, Iterable

from .notifications import suppress_company_history

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
# deliberately: since 1e, creating a pair has a SECOND obligation — recording the
# company's stored history as already-notified for that user — and a bulk INSERT
# that quietly skipped it would be a loaded gun left in the module, aimed at
# whichever future caller reached for the convenient one. There is now no way to
# create a tracking row without the suppression, because there is only one
# statement and one function around it. (The lesson that statement carried is
# worth keeping even though it is gone: SQLite cannot parse
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
# from "I already was" — a distinction that is load bearing rather than
# cosmetic here, because it is what decides whether history gets suppressed.
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
    """Start watching these companies, and do not deliver their history.

    Returns the ids genuinely newly tracked; one already tracked contributes
    nothing, exactly as track_all_companies reports.

    THE HISTORY SUPPRESSION IS THE WHOLE REASON THIS IS A FUNCTION AND NOT AN
    INSERT. A company in the catalog has stored jobs — hundreds of them, open,
    ordinary, never emailed to anybody. Since Phase 1e the digest queue is "open,
    in a company you watch, not back catalogue, not yet sent to you", so the
    instant this row exists every one of those jobs is in this person's queue and
    their first digest is an archive dump. That is the cry-wolf failure this
    codebase spends its guards on, arriving through the one door the silent seed
    does not cover: jobs.seeded stops a company's FIRST-EVER crawl reaching
    anyone, and says nothing about the weeks of ordinary crawling after it.

    So the pair and its suppression are written in ONE TRANSACTION. Not as
    tidiness: between the two there is a state where the user tracks the company
    and the history is not yet recorded, and the hourly digest runs in that state
    if the process dies there.

    ONLY ON A GENUINE INSERT, AND THIS IS THE SHARP EDGE. Suppression records
    every job the company has RIGHT NOW as already-notified. Running it against a
    pair that already exists would swallow the jobs that arrived since — the
    person's actual queue — and silently. So the suppression is driven by what
    RETURNING gave back, which the table's UNIQUE makes exact: a company already
    tracked yields no row, so nothing is suppressed for it, so re-running this is
    genuinely idempotent rather than idempotent-looking.

    Untrack and re-track is the case that proves the design rather than a corner
    of it: the pair is created again, so the suppression runs again, over a
    catalogue that has moved on — and every job it names is either already
    recorded (no-op) or is history this person genuinely never asked to hear
    about. Neither can produce an email.
    """
    ids = list(company_ids)
    if not ids:
        return []

    tracked: list[int] = []
    async with conn.transaction():
        for company_id in ids:
            rows = await conn.fetch(TRACK_ONE_COMPANY_SQL, user_id, company_id)
            if not rows:
                continue  # already tracked: its history was suppressed back then
            tracked.append(rows[0]["company_id"])
            await suppress_company_history(conn, user_id, company_id)

    return tracked


async def track_all_companies(conn, user_id: Any) -> list[int]:
    """Track every catalog company for this user. Returns the ids newly inserted.

    An empty list means every company was already tracked, which is the ordinary
    result of a second run — not an error and not a no-op to report as a failure.

    Goes through track_companies, so the history suppression cannot be skipped by
    reaching for the migration path instead of the ordinary one. That matters
    most for the one caller that exists: scripts/backfill_user_companies.py,
    which is how a SECOND user would be handed the catalog — and handing someone
    fifty companies' stored history without suppression is precisely the archive
    dump described above, aimed at the newest account.

    A company inserted into the catalog between the read below and the inserts is
    simply not tracked by this run. That window is not new and is not an
    oversight: scripts/backfill_user_companies.py gates on coverage of a snapshot
    taken before its insert for exactly this reason — the discovery sweep can add
    a company mid-run, and re-running picks it up.
    """
    return await track_companies(conn, user_id, await catalog_company_ids(conn))
