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

The SQL lives here rather than in the backfill script so the statement the tests
exercise is the statement production runs — the discipline
tests/test_applied_column.py established for the crawler's writes.
"""

from __future__ import annotations

from typing import Any

# Every company in the catalog, tracked for one user, in one statement.
#
# INSERT ... SELECT rather than a loop: the set of companies is decided by the
# database at the moment the statement runs, so there is no window between
# "which companies are there" and "track them" for the two answers to disagree.
#
# ON CONFLICT (user_id, company_id) DO NOTHING is what makes tracking idempotent,
# and it is the table's UNIQUE constraint doing the work rather than a check in
# Python — the same idiom crawler/discover.TRACK_SQL uses to make "add a company"
# safe to re-run. RETURNING company_id therefore yields a row only on a genuine
# insert, so a caller can count what it actually changed rather than what it
# attempted, exactly as scripts/seed_user.py and
# scripts/backfill_preferences_user.py do.
#
# `WHERE TRUE` is not a filter and is not a placeholder for one — it selects
# every company in the catalog, and in particular does NOT filter on
# companies.tracked (see the module docstring for why those are different
# questions). It is there because `INSERT ... SELECT ... ON CONFLICT` is
# genuinely ambiguous to a SQL parser: `ON` can begin a join constraint on the
# SELECT rather than the conflict clause. Postgres resolves it; SQLite reports a
# syntax error and documents `WHERE true` as the disambiguator. Written this way,
# the statement the tests run through the SQLite shim is the statement production
# runs, rather than a paraphrase of it — which is the whole point of keeping the
# SQL here instead of in the script.
TRACK_ALL_COMPANIES_SQL = """
INSERT INTO user_companies (user_id, company_id)
SELECT $1, id FROM companies WHERE TRUE
ON CONFLICT (user_id, company_id) DO NOTHING
RETURNING company_id
"""

# The catalog as a set of ids. Read before the insert by the backfill, so that
# what the migration is responsible for is a snapshot rather than a moving
# target — see scripts/backfill_user_companies.py for why that matters.
CATALOG_IDS_SQL = "SELECT id FROM companies ORDER BY id"

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


async def track_all_companies(conn, user_id: Any) -> list[int]:
    """Track every catalog company for this user. Returns the ids newly inserted.

    An empty list means every company was already tracked, which is the ordinary
    result of a second run — not an error and not a no-op to report as a failure.
    """
    return [row["company_id"] for row in await conn.fetch(TRACK_ALL_COMPANIES_SQL, user_id)]
