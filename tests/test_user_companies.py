"""The per-user watch list — and, above all, what it must not touch.

user_companies is the schema behind "crawl once, fan out". The half that is easy
to get right is the join itself: a unique pair, a backfill that can be re-run,
two cascades. The half worth a test file is the *absence* — the crawl still
selects the entire catalog and has never heard of this table, so that adding a
per-user notion cannot quietly turn the crawler into something that only fetches
boards somebody currently wants.

That invariant is asserted three ways here, because it can be broken in three
places: in the statement (a JOIN or a WHERE added to COMPANIES_SQL), in the code
around it (a module on the crawl path importing the watch list), and in what the
selection actually returns. The last one is the only one a rewrite cannot dodge.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from conftest import TEST_USER_ID
from db.watchlist import (
    TRACK_ALL_COMPANIES_SQL,
    catalog_company_ids,
    count_tracked,
    track_all_companies,
    tracked_company_ids,
)
from scripts.backfill_user_companies import uncovered

PROJECT_ROOT = Path(__file__).resolve().parent.parent

OTHER_USER_ID = "00000000-0000-4000-8000-000000000002"


async def catalog(conn, n: int = 3) -> list[int]:
    """A catalog of n companies. Returns their ids."""
    for i in range(1, n + 1):
        await conn.add_company(i, f"Company {i}")
    return list(range(1, n + 1))


# --- the join itself ------------------------------------------------------


async def test_tracking_the_same_company_twice_is_a_no_op(conn):
    """The second run of the production statement inserts nothing.

    Not "inserts a harmless duplicate" — inserts nothing, and RETURNING says so,
    which is what lets the backfill report "already tracking N" rather than
    guessing from a count that would look the same either way.
    """
    await catalog(conn, 3)
    user = await conn.add_user()

    first = await track_all_companies(conn, user)
    second = await track_all_companies(conn, user)

    assert sorted(first) == [1, 2, 3]
    assert second == []
    assert await count_tracked(conn, user) == 3


async def test_the_unique_constraint_is_what_enforces_it(conn):
    """The idempotence is the table's, not the ON CONFLICT clause's alone.

    A statement without the clause must raise. Otherwise "tracking is idempotent"
    would be a property of one carefully-written INSERT, and the next writer —
    the Phase 3 company picker — would have to know to copy it.
    """
    await catalog(conn, 1)
    user = await conn.add_user()
    await conn.track_company(user, 1)

    with pytest.raises(sqlite3.IntegrityError):
        await conn.track_company(user, 1)


async def test_two_users_may_track_the_same_company(conn):
    """The uniqueness is per pair, not per company — the whole point of the table.

    One shared catalog row, watched by two people, crawled once. If this failed,
    "crawl once, fan out" would be unrepresentable.
    """
    await catalog(conn, 2)
    a = await conn.add_user()
    b = await conn.add_user(OTHER_USER_ID, "other@example.test")

    await conn.track_company(a, 1)
    await conn.track_company(b, 1)

    assert await tracked_company_ids(conn, a) == [1]
    assert await tracked_company_ids(conn, b) == [1]


# --- the backfill ---------------------------------------------------------


async def test_backfill_tracks_exactly_the_catalog_that_exists(conn):
    """Every company, and only companies that exist. Behaviour unchanged.

    `tracked = FALSE` is deliberately not filtered on: it is the crawl-exclusion
    axis, not a statement about what a person wants alerts for. Before this table
    the digest considered every company's jobs regardless of that flag, so the
    backfill has to write down exactly that.
    """
    await conn.add_company(1, "Tracked Co", tracked=True)
    await conn.add_company(2, "Untracked Co", tracked=False)
    user = await conn.add_user()

    await track_all_companies(conn, user)

    assert await tracked_company_ids(conn, user) == await catalog_company_ids(conn) == [1, 2]


async def test_backfill_is_idempotent_and_reports_nothing_new(conn):
    """A re-run tracks nothing new and leaves the row set identical."""
    await catalog(conn, 3)
    user = await conn.add_user()

    await track_all_companies(conn, user)
    before = await conn.tracking_rows()

    assert await track_all_companies(conn, user) == []
    assert await conn.tracking_rows() == before


async def test_backfill_picks_up_a_company_added_later(conn):
    """A second run tracks what the first could not have seen.

    The counterpart to idempotence: "re-running changes nothing" must not mean
    "re-running is useless". This is also the documented fix for the mid-run
    catalog growth the gate below refuses to fail on.
    """
    await catalog(conn, 2)
    user = await conn.add_user()
    await track_all_companies(conn, user)

    await conn.add_company(3, "Late Arrival")

    assert await track_all_companies(conn, user) == [3]
    assert await tracked_company_ids(conn, user) == [1, 2, 3]


# --- the gate, and the catalog race it must not cry wolf on ---------------


def test_gate_passes_when_the_snapshot_is_covered():
    assert uncovered([1, 2, 3], [1, 2, 3]) == []


def test_gate_fails_and_names_what_is_missing():
    """A rolled-back insert, or a write against the wrong user, fails loudly."""
    assert uncovered([1, 2, 3], [1, 3]) == [2]
    assert uncovered([1, 2, 3], []) == [1, 2, 3]


def test_gate_ignores_a_company_added_after_the_snapshot():
    """The race the gate is built around.

    A company inserted by the discovery sweep between this run's INSERT and its
    read-back is a company this run never saw and was never answerable for. A
    gate that re-counted the catalog fresh would read 3 tracked against 4 in the
    catalog and fail a migration that did its job perfectly. Coverage of the
    snapshot cannot: id 4 is not in the snapshot, so it cannot be missing from it.
    """
    catalog_before = [1, 2, 3]
    tracked_after = [1, 2, 3]
    catalog_after = [1, 2, 3, 4]  # the sweep landed mid-run

    assert uncovered(catalog_before, tracked_after) == []
    assert len(tracked_after) != len(catalog_after)  # a fresh re-count WOULD have failed


def test_gate_still_fails_when_the_snapshot_is_uncovered_and_the_catalog_grew():
    """Tolerating the race must not tolerate a genuinely broken backfill."""
    assert uncovered([1, 2, 3], [1, 2]) == [3]


# --- both cascades --------------------------------------------------------


async def test_deleting_a_user_removes_only_their_tracking_rows(conn):
    conn.enforce_foreign_keys()
    await catalog(conn, 3)
    a = await conn.add_user()
    b = await conn.add_user(OTHER_USER_ID, "other@example.test")
    for cid in (1, 2, 3):
        await conn.track_company(a, cid)
    await conn.track_company(b, 2)

    await conn.execute("DELETE FROM users WHERE id = $1", a)

    assert await conn.tracking_rows() == {(b, 2)}
    # and the shared catalog is untouched — a user leaving takes no company with them
    assert await catalog_company_ids(conn) == [1, 2, 3]


async def test_deleting_a_company_removes_every_users_row_for_it(conn):
    conn.enforce_foreign_keys()
    await catalog(conn, 3)
    a = await conn.add_user()
    b = await conn.add_user(OTHER_USER_ID, "other@example.test")
    for cid in (1, 2, 3):
        await conn.track_company(a, cid)
        await conn.track_company(b, cid)

    await conn.execute("DELETE FROM companies WHERE id = $1", 2)

    assert await conn.tracking_rows() == {(a, 1), (a, 3), (b, 1), (b, 3)}
    # and both users survive their company being dropped from the catalog
    assert await conn.fetchval("SELECT count(*) FROM users") == 2


async def test_an_orphan_tracking_row_cannot_be_created(conn):
    """The other half of CASCADE: the FK also refuses a row pointing at nothing.

    Cascading on delete keeps orphans from appearing later; this keeps them from
    being written in the first place. Both matter to the 1e fan-out, which joins
    user_companies to companies and to public.users and would otherwise silently
    drop whatever the join could not resolve.
    """
    conn.enforce_foreign_keys()
    await catalog(conn, 1)
    user = await conn.add_user()

    with pytest.raises(sqlite3.IntegrityError):
        await conn.track_company(user, 999)  # no such company
    with pytest.raises(sqlite3.IntegrityError):
        await conn.track_company(OTHER_USER_ID, 1)  # no such user


# --- THE INVARIANT: the crawl path has never heard of this table ----------

# Every module a crawl actually executes, from the entrypoint down. Listed by
# hand rather than globbed, so adding a module to the crawl path is a deliberate
# edit here and not something a wildcard silently absorbs.
CRAWL_PATH_FILES = [
    "scripts/crawl.py",
    "crawler/loop.py",
    "crawler/store.py",
    "crawler/diff.py",
    "crawler/enrich.py",
    "fetchers/http.py",
    "fetchers/normalize.py",
    "fetchers/tier1.py",
    "fetchers/tier3.py",
    "fetchers/workday.py",
]

FORBIDDEN_ON_THE_CRAWL_PATH = ("user_companies", "watchlist", "tracked_company_ids")

# The company selection, as it stood before user_companies existed and as it must
# still stand. Whitespace-normalised on both sides; nothing else is.
EXPECTED_COMPANIES_SQL = (
    "SELECT id, name, ats, token, config FROM companies WHERE tracked = TRUE ORDER BY id"
)


def normalise(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip()


def test_the_crawl_still_selects_the_whole_catalog():
    """The statement, character for character modulo whitespace.

    A join to user_companies, an EXISTS, or a WHERE naming a user all fail here —
    including the well-meaning kind that only *looks* like an optimisation. The
    crawl is not allowed to become conditional on somebody wanting the results:
    scripts/crawl.py decides silent seeding from a company's stored row count, so
    a board that went untracked for a month would come back and re-alert its
    entire back catalogue the day someone added it.
    """
    from scripts.crawl import COMPANIES_SQL

    assert normalise(COMPANIES_SQL) == EXPECTED_COMPANIES_SQL


@pytest.mark.parametrize("relative_path", CRAWL_PATH_FILES)
@pytest.mark.parametrize("forbidden", FORBIDDEN_ON_THE_CRAWL_PATH)
def test_no_crawl_path_module_mentions_the_watch_list(relative_path, forbidden):
    """Not "does not filter on it" — does not mention it at all.

    The same discipline tests/test_applied_column.py applies to the dashboard-owned
    columns: a name absent from the source cannot be read, imported, joined or
    accidentally reintroduced by an edit that looked local.
    """
    source = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
    assert forbidden not in source, f"{relative_path} must not reference {forbidden!r}"


async def test_company_selection_is_indifferent_to_who_tracks_what(conn):
    """Behavioural, and the one a rewrite cannot dodge.

    Three tracked companies in the catalog, the user watching exactly one. The
    crawl's own statement still returns all three — the fan-out narrows later,
    the fetching never does.
    """
    from scripts.crawl import COMPANIES_SQL

    await catalog(conn, 3)
    user = await conn.add_user()
    await conn.track_company(user, 2)

    selected = [row["id"] for row in await conn.fetch(COMPANIES_SQL)]

    assert selected == [1, 2, 3]
    assert await tracked_company_ids(conn, user) == [2]
