"""The crawler must never write `applied` or `applied_at`.

Every other column on `jobs` belongs to the crawler, because every other column
records something a fetch observed. These two record something only a person
knows — I applied to this, at this moment — and no crawl can rediscover that. A
re-sighting that cleared the tick would silently delete the one piece of state in
this database that cannot be recomputed from the boards.

The protection is structural: the two production statements that write to `jobs`
during a crawl name their columns explicitly, and neither list contains these
two. This file asserts that twice over, in the two ways it can be wrong:

  - textually, against the SQL constants, so a column added to an UPDATE by
    someone extending it fails here rather than in production;
  - behaviourally, by ticking the flag and then running whole crawl cycles over
    the real store functions, so a future rewrite that abandons those constants
    is still caught by what it does to the row.

The behavioural half is the one that matters, and it is deliberately run through
`crawl()` rather than through `upsert_seen()` alone, so it also covers mark_new,
bump_missing and close_stale — the three other writers — in one pass.
"""

from __future__ import annotations

import pytest

from conftest import fetcher_for, job, load_board
from crawler.loop import Company, crawl
from crawler.store import MISS_LIMIT, UPSERT_SEEN
from notify.store import SAVE_DESCRIPTION_FACTS_SQL, save_description_facts

COMPANY = Company(id=1, name="testco", ats="greenhouse", token="testco", config={})

DASHBOARD_OWNED = ("applied", "applied_at")

APPLIED_AT = "2026-07-27T09:30:00+00:00"


async def run(conn, boards, seeding=False):
    return await crawl(conn, COMPANY, seeding=seeding, fetchers=fetcher_for(*boards))


async def row(conn, canonical_id: str) -> dict:
    return (await conn.snapshot())[canonical_id]


# --- the statements themselves --------------------------------------------


@pytest.mark.parametrize("column", DASHBOARD_OWNED)
def test_upsert_seen_never_names_a_dashboard_column(column):
    """The crawl's one write to jobs does not mention these columns at all.

    Not "does not set them to the wrong value" — does not mention them. A column
    absent from both the INSERT list and the ON CONFLICT DO UPDATE clause keeps
    whatever it already held, on every sighting, forever.
    """
    assert column not in UPSERT_SEEN


@pytest.mark.parametrize("column", DASHBOARD_OWNED)
def test_enrichment_never_names_a_dashboard_column(column):
    """Enrichment writes five description facts and nothing else.

    Same argument as the lifecycle columns already documented on this statement:
    the safety is the column list, so the column list is what gets asserted.
    """
    assert column not in SAVE_DESCRIPTION_FACTS_SQL


# --- and what they actually do to a ticked row ----------------------------


async def test_applied_survives_an_ordinary_re_sighting(conn):
    """The common case: the job is still on the board tomorrow. The tick stays."""
    board = load_board()
    await run(conn, [board], seeding=True)

    target = (await conn.fetch("SELECT canonical_id FROM jobs LIMIT 1"))[0]["canonical_id"]
    await conn.execute(
        "UPDATE jobs SET applied = 1, applied_at = $1 WHERE canonical_id = $2",
        APPLIED_AT,
        target,
    )

    for _ in range(3):
        await run(conn, [board], seeding=False)

    after = await row(conn, target)
    assert after["applied"] == 1
    assert after["applied_at"] == APPLIED_AT
    assert after["last_seen_at"] is not None  # the row WAS refreshed, just not there


async def test_applied_survives_an_edited_posting(conn):
    """A recruiter renaming the req rewrites title, location and city — not this."""
    board = load_board()
    await run(conn, [board], seeding=True)

    target_id = f"greenhouse:{COMPANY.id}:{board[0]['native_id']}"
    await conn.execute(
        "UPDATE jobs SET applied = 1, applied_at = $1 WHERE canonical_id = $2",
        APPLIED_AT,
        target_id,
    )

    edited = list(board)
    edited[0] = {**edited[0], "title": "Renamed Role", "location": "Hyderabad, India"}
    await run(conn, [edited], seeding=False)

    after = await row(conn, target_id)
    assert after["title"] == "Renamed Role"  # the crawler did update the row
    assert after["applied"] == 1
    assert after["applied_at"] == APPLIED_AT


async def test_applied_survives_closing_and_reopening(conn):
    """Applied to a job, the board pulls it, the board puts it back. Still applied.

    This is the path that touches every remaining writer: bump_missing walks
    miss_count up, close_stale flips status, and the upsert reopens it. None of
    the three names the column, so the tick rides through all of them.
    """
    board = load_board()
    await run(conn, [board], seeding=True)

    target = (await conn.fetch("SELECT canonical_id FROM jobs LIMIT 1"))[0]["canonical_id"]
    await conn.execute(
        "UPDATE jobs SET applied = 1, applied_at = $1 WHERE canonical_id = $2",
        APPLIED_AT,
        target,
    )
    short = [j for j in board if f"greenhouse:{COMPANY.id}:{j['native_id']}" != target]

    for _ in range(MISS_LIMIT):
        await run(conn, [short], seeding=False)
    closed = await row(conn, target)
    assert closed["status"] == "closed"  # the lifecycle did move
    assert closed["applied"] == 1
    assert closed["applied_at"] == APPLIED_AT

    await run(conn, [board], seeding=False)
    reopened = await row(conn, target)
    assert reopened["status"] == "open"
    assert reopened["applied"] == 1
    assert reopened["applied_at"] == APPLIED_AT


async def test_a_brand_new_row_arrives_not_applied(conn):
    """The default is FALSE / NULL, and the crawler has no way to make it anything else."""
    board = load_board()
    await run(conn, [board], seeding=True)
    await run(conn, [board + [job("NEW-1")]], seeding=False)

    fresh = await row(conn, f"greenhouse:{COMPANY.id}:NEW-1")
    assert fresh["applied"] == 0
    assert fresh["applied_at"] is None


async def test_enrichment_does_not_clear_an_applied_row(conn):
    """Reading a description writes six facts over a row that may be ticked.

    Six since the work-mode slice, and the arity is checked here rather than
    only in test_description.py because THIS is the test that guards ownership:
    whatever else the enrichment write grows to name, it must never name
    `applied` or `applied_at`.
    """
    await conn.add_company(1, "Testco")
    await conn.add_job("j1", title="Backend Engineer", applied=True, applied_at=APPLIED_AT)

    await save_description_facts(conn, [("j1", 6, True, False, "remote", 18, 24, True)])

    after = await row(conn, "j1")
    assert after["min_experience"] == 6  # the write happened
    assert after["desc_checked"] == 1
    assert after["applied"] == 1
    assert after["applied_at"] == APPLIED_AT


async def test_whole_crawl_leaves_every_column_but_the_lifecycle_alone(conn):
    """Belt and braces: diff the row before and after, and name what may differ.

    A column-by-column comparison rather than two asserts, because the failure
    this guards against is a future column being added to UPSERT_SEEN by someone
    who never read this file. Anything outside the allow-list changing is a
    finding, not just `applied` changing.
    """
    board = load_board()
    await run(conn, [board], seeding=True)

    target = (await conn.fetch("SELECT canonical_id FROM jobs LIMIT 1"))[0]["canonical_id"]
    await conn.execute(
        "UPDATE jobs SET applied = 1, applied_at = $1 WHERE canonical_id = $2",
        APPLIED_AT,
        target,
    )
    before = await row(conn, target)

    await run(conn, [board], seeding=False)
    after = await row(conn, target)

    # last_seen_at is the only thing an unchanged re-sighting is allowed to move.
    changed = {k for k in before if before[k] != after[k]}
    assert changed == {"last_seen_at"}
