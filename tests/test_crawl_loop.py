"""Two-snapshot scenarios run end to end against the real SQL.

Each test spells out a sequence of board states and asserts what the jobs table
looks like afterwards. The fetchers are stubs; everything below them —
diff_snapshots, upsert_seen, mark_new, bump_missing, close_stale, and all three
guards — is the production code path.

The recurring assertion is not "the right rows exist" but "no row acquired
seeded=FALSE that should not have". That flag is what the digest queue reads, so a
row that lands there wrongly is a false alert, which is the failure mode this
whole step exists to prevent.
"""

from __future__ import annotations

import pytest

from conftest import fetcher_for, job, load_board, raising_fetcher
from crawler.loop import Company, crawl
from crawler.store import MISS_LIMIT

COMPANY = Company(id=1, name="testco", ats="greenhouse", token="testco", config={})


async def run(conn, boards, seeding=False, ats="greenhouse"):
    """Crawl once against the next board in the sequence."""
    return await crawl(conn, COMPANY, seeding=seeding, fetchers=fetcher_for(*boards, ats=ats))


async def queued(conn) -> set[str]:
    """Ids a digest could reach: stored by an ordinary crawl, not back catalogue.

    `seeded = 0` is the crawl-path half of the queue and the only half this file
    can ask about — whether a given PERSON gets one of these depends on their
    watch list and on what they have already been told, and the crawl knows
    neither. Until Phase 1g this read `notified = 0`, which conflated the two.
    """
    rows = await conn.fetch("SELECT canonical_id FROM jobs WHERE seeded = 0")
    return {r["canonical_id"] for r in rows}


# --- (a) the silent seed --------------------------------------------------


async def test_seed_run_stores_every_job_as_back_catalogue(conn):
    """A first-ever crawl records the whole back catalogue and alerts on none.

    This is the guard against sending 3000 emails on day one. Every job on the
    board looks new because we have never seen the board — so the seed run marks
    them and the queue comes out empty.

    `seeded` is what says so: "this row is back catalogue", a fact about this
    crawl — user-blind, permanent, and the clause the digest queue reads. It was
    written alongside the old global `notified` boolean from 1e until 1g, which
    is what let 1g drop that column without changing what any digest contains.
    """
    board = load_board()
    result = await run(conn, [board], seeding=True)

    rows = await conn.snapshot()
    assert result.fetched == len(board) == 11
    assert result.truly_new == 11
    assert len(rows) == 11
    assert all(r["seeded"] == 1 for r in rows.values())
    assert all(r["status"] == "open" and r["miss_count"] == 0 for r in rows.values())
    assert await queued(conn) == set()  # zero would-be notifications

    # The city label is computed and stored, and is not used to drop anything:
    # the "Mumbai, India" rows are stored exactly like the Bengaluru ones. They
    # label `other` now rather than `india-unclear` — Mumbai is a named city and
    # naming one is an answer — and STORING them anyway is the point of the
    # assertion. Store loosely; the label decides nothing until the digest.
    assert {r["city"] for r in rows.values()} == {"blr", "india-unclear", "other"}
    assert sum(1 for r in rows.values() if r["city"] != "blr") == 5


async def test_seed_never_stores_a_description(conn):
    """Metadata only. The column does not exist, and nothing tries to add it."""
    await run(conn, [load_board()], seeding=True)
    columns = {row[1] for row in await conn.fetch("SELECT * FROM pragma_table_info('jobs')")}
    assert not columns & {"description", "content", "body"}


# --- (b) a quiet second run ----------------------------------------------


async def test_identical_second_snapshot_changes_nothing(conn):
    """The common case: the board is the same today. Nothing new, nothing closed."""
    board = load_board()
    await run(conn, [board], seeding=True)
    before = await conn.snapshot()

    result = await run(conn, [board], seeding=False)
    after = await conn.snapshot()

    assert result.truly_new == 0
    assert result.reappeared == 11
    assert result.missing == 0
    assert result.closed == 0
    assert await queued(conn) == set()  # no queue-eligible row was created
    assert set(after) == set(before)
    # last_seen_at moved; first_seen_at did not.
    assert all(after[k]["first_seen_at"] == before[k]["first_seen_at"] for k in before)
    assert all(after[k]["last_seen_at"] != before[k]["last_seen_at"] for k in before)


# --- (c) one genuinely new job -------------------------------------------


async def test_one_added_job_is_the_only_thing_queued(conn):
    """Post-seed, exactly the added id is new and exactly it lands in the queue."""
    board = load_board()
    added = job("NEW-1", title="Staff Engineer", location="Pune, India")

    await run(conn, [board], seeding=True)
    result = await run(conn, [board + [added]], seeding=False)

    new_id = f"greenhouse:{COMPANY.id}:NEW-1"
    rows = await conn.snapshot()
    assert result.truly_new == 1
    assert result.reappeared == 11
    assert await queued(conn) == {new_id}
    assert rows[new_id]["seeded"] == 0
    # Not back catalogue: this is the one row a digest is allowed to reach. A
    # non-seeding crawl writing seeded=TRUE would hide every genuinely new job
    # from every user, permanently and silently — the exact inverse of the flood
    # the flag exists to prevent, and the reason both directions are asserted.
    assert rows[new_id]["seeded"] == 0
    assert all(rows[k]["seeded"] == 1 for k in rows if k != new_id)
    assert rows[new_id]["city"] == "pune"
    assert rows[new_id]["status"] == "open"
    assert rows[new_id]["first_seen_at"] is not None


# --- (d) a job disappears -------------------------------------------------


async def test_removed_job_closes_only_on_the_third_consecutive_miss(conn):
    """miss_count walks 1, 2, 3 and the row closes on the third, never before.

    Closing on the first absence is what makes a board's one flaky fetch turn
    into a wave of "new" jobs the next day.
    """
    board = load_board()
    await run(conn, [board], seeding=True)

    dropped_id = (await conn.fetch("SELECT canonical_id FROM jobs LIMIT 1"))[0]["canonical_id"]
    short = [j for j in board if f"greenhouse:{COMPANY.id}:{j['native_id']}" != dropped_id]

    for expected_miss in range(1, MISS_LIMIT + 1):
        result = await run(conn, [short], seeding=False)
        row = (await conn.snapshot())[dropped_id]

        assert result.missing == 1
        assert row["miss_count"] == expected_miss
        if expected_miss < MISS_LIMIT:
            assert row["status"] == "open", f"closed too early, after {expected_miss} miss(es)"
            assert result.closed == 0
        else:
            assert row["status"] == "closed"
            assert result.closed == 1

    # The row is kept, not deleted — that memory is what stops a reappearance
    # from reading as news.
    assert len(await conn.snapshot()) == 11


async def test_a_closed_job_is_not_counted_missing_again(conn):
    """Once closed, miss_count stops climbing; it is out of the open set."""
    board = load_board()
    await run(conn, [board], seeding=True)
    dropped_id = (await conn.fetch("SELECT canonical_id FROM jobs LIMIT 1"))[0]["canonical_id"]
    short = [j for j in board if f"greenhouse:{COMPANY.id}:{j['native_id']}" != dropped_id]

    for _ in range(MISS_LIMIT + 2):
        result = await run(conn, [short], seeding=False)

    row = (await conn.snapshot())[dropped_id]
    assert row["status"] == "closed"
    assert row["miss_count"] == MISS_LIMIT
    assert result.missing == 0  # no longer in known_open, so not re-counted


# --- (e) the flap ---------------------------------------------------------


async def test_flapping_job_is_never_closed_and_never_re_alerted(conn):
    """Out at t1, back at t2: miss_count resets to 0 and no notification appears.

    A vendor cache serving one stale page must cost nothing.
    """
    board = load_board()
    await run(conn, [board], seeding=True)

    flapper_id = (await conn.fetch("SELECT canonical_id FROM jobs LIMIT 1"))[0]["canonical_id"]
    short = [j for j in board if f"greenhouse:{COMPANY.id}:{j['native_id']}" != flapper_id]

    await run(conn, [short], seeding=False)
    assert (await conn.snapshot())[flapper_id]["miss_count"] == 1

    result = await run(conn, [board], seeding=False)
    row = (await conn.snapshot())[flapper_id]

    assert row["miss_count"] == 0
    assert row["status"] == "open"
    assert result.truly_new == 0
    assert result.closed == 0
    assert await queued(conn) == set()


async def test_flap_can_repeat_forever_without_closing(conn):
    """Alternating present/absent never accumulates to MISS_LIMIT."""
    board = load_board()
    await run(conn, [board], seeding=True)
    flapper_id = (await conn.fetch("SELECT canonical_id FROM jobs LIMIT 1"))[0]["canonical_id"]
    short = [j for j in board if f"greenhouse:{COMPANY.id}:{j['native_id']}" != flapper_id]

    for _ in range(5):
        await run(conn, [short], seeding=False)
        await run(conn, [board], seeding=False)

    row = (await conn.snapshot())[flapper_id]
    assert row["status"] == "open"
    assert row["miss_count"] == 0
    assert await queued(conn) == set()


# --- (f) GUARD 2: the truncated fetch -------------------------------------


async def test_partial_fetch_writes_nothing_at_all(conn):
    """A board answering with under half its known-open set is refused whole.

    No error, valid JSON, wrong answer — a paginator that died mid-way. Nothing
    downstream can tell that from a mass closure, so the fetch is dropped and
    the table is left byte-identical.
    """
    board = load_board()
    await run(conn, [board], seeding=True)
    before = await conn.snapshot()

    result = await run(conn, [board[:4]], seeding=False)  # 4 of 11 == 36%
    after = await conn.snapshot()

    assert result.guard_tripped
    assert result.guard.startswith("partial_fetch")
    assert result.truly_new == result.missing == result.closed == 0
    assert after == before  # no insert, no update, no close
    assert await queued(conn) == set()


async def test_partial_fetch_floor_is_not_tripped_by_ordinary_churn(conn):
    """Half is a floor, not a hair trigger — a real 6-of-11 board still crawls."""
    board = load_board()
    await run(conn, [board], seeding=True)

    result = await run(conn, [board[:6]], seeding=False)  # 6 of 11 == 55%

    assert not result.guard_tripped
    assert result.missing == 5
    assert result.closed == 0  # still only the first miss


async def test_empty_board_on_a_seed_run_is_allowed_to_proceed(conn):
    """GUARD 2 needs a baseline. A company with no open rows has none.

    Without the `open_ids and` check, a first crawl could never write anything.
    """
    result = await run(conn, [[]], seeding=True)
    assert not result.guard_tripped
    assert result.fetched == 0


# --- (g) GUARD 1: the fetch that raised -----------------------------------


@pytest.mark.parametrize("exc", [RuntimeError("boom"), TimeoutError(), KeyError("ats")])
async def test_fetch_error_leaves_state_completely_unchanged(conn, exc):
    """Any exception aborts before the first write.

    A 500 must never read as "all jobs closed" — three of those in a row would
    close a board and the recovery run would re-alert every job on it.
    """
    board = load_board()
    await run(conn, [board], seeding=True)
    before = await conn.snapshot()

    result = await crawl(
        conn, COMPANY, seeding=False, fetchers=raising_fetcher(exc)
    )

    assert result.guard_tripped
    assert result.guard.startswith("fetch_error")
    assert await conn.snapshot() == before
    assert await queued(conn) == set()


async def test_unknown_ats_is_a_fetch_error_not_a_crash(conn):
    """A company row pointing at an ATS with no fetcher trips GUARD 1."""
    unknown = Company(id=2, name="mystery", ats="workday", token="acme", config={})
    result = await crawl(conn, unknown, seeding=True, fetchers={})
    assert result.guard == "fetch_error:KeyError"
    assert await conn.snapshot() == {}


# --- (h) the reopened job -------------------------------------------------


async def test_closed_job_that_comes_back_is_reopened_not_re_notified(conn):
    """Reopen: status back to open, miss_count zeroed, `seeded` left alone.

    This is the payoff of keeping closed rows around. The id is in
    known_closed, so diff_snapshots calls it `reappeared`, and reappeared never
    reaches mark_new.
    """
    board = load_board()
    await run(conn, [board], seeding=True)
    gone_id = (await conn.fetch("SELECT canonical_id FROM jobs LIMIT 1"))[0]["canonical_id"]
    short = [j for j in board if f"greenhouse:{COMPANY.id}:{j['native_id']}" != gone_id]

    for _ in range(MISS_LIMIT):
        await run(conn, [short], seeding=False)
    closed_row = (await conn.snapshot())[gone_id]
    assert closed_row["status"] == "closed"

    result = await run(conn, [board], seeding=False)
    reopened = (await conn.snapshot())[gone_id]

    assert result.truly_new == 0
    assert result.reappeared == 11
    assert reopened["status"] == "open"
    assert reopened["miss_count"] == 0
    # Still back catalogue. An upsert clearing `seeded` would hand a whole seeded
    # board to every user the first time one of its jobs flapped, which is the
    # failure this assertion has guarded in one column or another since Step 3.
    assert reopened["seeded"] == 1
    assert reopened["first_seen_at"] == closed_row["first_seen_at"]  # not restamped
    assert await queued(conn) == set()


async def test_job_reopens_without_losing_its_queue_position(conn):
    """A job that was new-but-unsent, then flapped, is still queued exactly once.

    seeded=FALSE must survive an upsert, or the job leaves every queue silently.
    """
    board = load_board()
    added = job("NEW-1")
    await run(conn, [board], seeding=True)
    await run(conn, [board + [added]], seeding=False)

    new_id = f"greenhouse:{COMPANY.id}:NEW-1"
    assert await queued(conn) == {new_id}

    await run(conn, [board], seeding=False)  # it vanishes for one fetch
    result = await run(conn, [board + [added]], seeding=False)  # and returns

    assert result.truly_new == 0  # not news a second time
    assert await queued(conn) == {new_id}  # still exactly one queued job
    assert (await conn.snapshot())[new_id]["miss_count"] == 0


# --- metadata upkeep ------------------------------------------------------


async def test_edited_job_updates_metadata_without_re_alerting(conn):
    """A recruiter fixing a title refreshes the row; it does not re-queue it."""
    board = load_board()
    await run(conn, [board], seeding=True)

    edited = list(board)
    edited[0] = {**edited[0], "title": "Renamed Role", "location": "Hyderabad, India"}
    result = await run(conn, [edited], seeding=False)

    row_id = f"greenhouse:{COMPANY.id}:{edited[0]['native_id']}"
    row = (await conn.snapshot())[row_id]
    assert result.truly_new == 0
    assert row["title"] == "Renamed Role"
    assert row["city"] == "hyd"
    assert await queued(conn) == set()


async def test_seeding_flag_only_affects_brand_new_rows(conn):
    """Post-seed truly_new rows queue; seed-run truly_new rows do not."""
    board = load_board()
    await run(conn, [board], seeding=True)
    assert await queued(conn) == set()

    await run(conn, [board + [job("A")]], seeding=False)
    await run(conn, [board + [job("A"), job("B")]], seeding=False)

    assert await queued(conn) == {
        f"greenhouse:{COMPANY.id}:A",
        f"greenhouse:{COMPANY.id}:B",
    }
