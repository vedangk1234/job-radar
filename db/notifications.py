"""Per-user notification state: who has been emailed about which job.

A ROW EXISTING IS THE DECISION
------------------------------
`user_job_notifications` holds one row per (user, job) pair that has been
emailed. There is no flag on it and no state in between: the row is there, or it
is not. Every statement in this module is written against that reading, which is
why none of them selects `notified_at` — that column is record-keeping (see the
table's comment block in db/schema.sql) and nothing branches on it.

WHY THE SQL LIVES HERE RATHER THAN IN THE BACKFILL SCRIPT
---------------------------------------------------------
The same discipline db/watchlist.py follows and tests/test_applied_column.py
established: the statement the tests exercise has to be the statement production
runs, not a paraphrase of it. The backfill in particular is the single most
dangerous statement in Phase 1 — getting it wrong re-emails a person's entire job
history in one send — so "the test ran the real INSERT" is worth more here than
anywhere else in the codebase.

The digest's own queue read is NOT here. It lives in notify/store.CANDIDATES_SQL,
because it is one statement joining jobs to companies and splitting it across two
modules to isolate four lines of predicate would make the query harder to read
than the coupling it removed. What lives here is everything that writes this
table, plus the two statements the backfill's gate is built from.

SQL portability note (same as crawler/store.py and notify/store.py): these stay
inside the subset Postgres and SQLite agree on, so the tests run them against the
in-memory shim rather than against a mock.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

# SQLite caps host parameters at 999 and Postgres at 65535; chunk well below both,
# exactly as crawler/store.py and notify/store.py do. This one matters more than
# either: the backfill's own INSERT ... SELECT never binds a list, but record_sent
# can be handed every match in a digest at once.
CHUNK = 400

# THE BACKFILL. The most dangerous statement in Phase 1.
#
# It writes down what the global boolean already meant while there was one user:
# "the single existing user was already emailed about these." Every job currently
# notified = TRUE gets one row for the user resolved from MY_EMAIL.
#
# IF THIS IS SKIPPED, OR RUNS AGAINST THE WRONG USER, OR HALF-FAILS, the first
# digest after Phase 1d finds every historical match uncovered and re-emails the
# lot — roughly 7,700 rows' worth of "you already knew this" in one send, which
# is precisely the cry-wolf failure that makes an inbox worthless. The gate in
# scripts/backfill_user_notifications.py exists so that outcome cannot be reached
# by believing a migration succeeded when it did not.
#
# INSERT ... SELECT rather than a Python loop, for the reason
# db/watchlist.TRACK_ALL_COMPANIES_SQL gives: the set is decided by the database
# at the instant the statement runs, so there is no window in which "which jobs
# are notified" and "record them" can disagree.
#
# ON CONFLICT (user_id, canonical_id) DO NOTHING is what makes this idempotent,
# and it is the table's own UNIQUE doing the work rather than a check in Python.
# RETURNING canonical_id therefore yields a row only on a genuine insert, so the
# script reports what it actually changed rather than what it attempted — the
# same idiom scripts/seed_user.py, crawler/discover.TRACK_SQL and
# scripts/backfill_preferences_user.py all use.
#
# notified_at is omitted so it takes its DEFAULT now(). That is the honest value:
# it means "when this state was recorded", and for these rows the recording IS
# this migration. Reconstructing a send date we do not have — from first_seen_at,
# or from the fifteen `notifications` receipts that cover 0.2% of these rows —
# would be inventing a fact, and db/schema.sql spells out why nothing may ever
# branch on it.
#
# No `WHERE TRUE` disambiguator is needed here, unlike TRACK_ALL_COMPANIES_SQL.
# That trick exists because SQLite cannot parse INSERT ... SELECT ... ON CONFLICT
# when the SELECT has no WHERE clause (it reads `ON` as beginning a join). This
# statement has a real predicate in that position, so it parses in both engines
# and the tests run it character for character.
BACKFILL_SQL = """
INSERT INTO user_job_notifications (user_id, canonical_id)
SELECT $1, j.canonical_id
  FROM jobs j
 WHERE j.notified = TRUE
ON CONFLICT (user_id, canonical_id) DO NOTHING
RETURNING canonical_id
"""

# Recording that a digest told this user about these jobs.
#
# Built as ONE multi-row VALUES per chunk rather than a statement per job. A
# digest is a few dozen matches and the database is in Tokyo; forty round trips
# inside the transaction that is already holding the boolean flip and the sent-log
# insert is forty chances for the send to be recorded slowly and half a reason for
# a lock to be held longer than the work needs. The dynamic build is the same
# idiom notify/store._placeholders already uses for its IN-lists.
#
# ON CONFLICT DO NOTHING rather than DO UPDATE: a second send of the same job to
# the same person is a no-op, not a re-stamping. Moving notified_at forward would
# be the only thing DO UPDATE could do, and nothing reads that value — so the
# choice is between a write that changes nothing and a write that changes nothing
# while looking like it might.
#
# RETURNING canonical_id yields a row only on a genuine insert, which is what
# lets notify/store.record_sent distinguish "this digest told you about 4 jobs"
# from "4 rows were handed to the database".
RECORD_NOTIFIED_TEMPLATE = """
INSERT INTO user_job_notifications (user_id, canonical_id)
VALUES {rows}
ON CONFLICT (user_id, canonical_id) DO NOTHING
RETURNING canonical_id
"""


def record_notified_sql(count: int) -> str:
    """The insert for `count` jobs: `VALUES ($1, $2), ($1, $3), ...`.

    $1 is the user and is repeated, which asyncpg handles natively and the test
    shim's _translate handles by design (it maps placeholders in order of
    appearance precisely so a repeated one works).
    """
    rows = ", ".join(f"($1, ${n})" for n in range(2, count + 2))
    return RECORD_NOTIFIED_TEMPLATE.format(rows=rows)

# THE GATE, HALF ONE — the safety property, and the one that actually protects
# the inbox.
#
# "How many jobs are marked notified but have no row for this user?" Zero is the
# only acceptable answer, because every job it counts is a job the next digest
# would consider fresh and re-send.
#
# This is strictly stronger than the count equality below and is not redundant
# with it: two counts can agree while the MEMBERSHIP is wrong (a row for the
# wrong job, or rows for a second user offsetting a genuine miss), and a count
# match on the wrong set is exactly the shape of a migration that looks green and
# re-spams anyway.
UNCOVERED_NOTIFIED_SQL = """
SELECT count(*)
  FROM jobs j
 WHERE j.notified = TRUE
   AND NOT EXISTS (
         SELECT 1
           FROM user_job_notifications n
          WHERE n.user_id = $1
            AND n.canonical_id = j.canonical_id
       )
"""

# THE GATE, HALF TWO — the arithmetic, printed with both numbers so a person can
# see them agree rather than being told that they did.
#
# Scoped to one user on purpose. A table-wide count would start disagreeing the
# moment a second user exists, which is the entire direction this phase is
# travelling in.
COUNT_MINE_SQL = "SELECT count(*) FROM user_job_notifications WHERE user_id = $1"

COUNT_NOTIFIED_JOBS_SQL = "SELECT count(*) FROM jobs WHERE notified = TRUE"


def _chunks(items: Sequence[str]) -> Iterable[Sequence[str]]:
    for start in range(0, len(items), CHUNK):
        yield items[start : start + CHUNK]


async def backfill_from_notified(conn, user_id: Any) -> list[str]:
    """Give this user a notification row for every job currently notified = TRUE.

    Returns the canonical_ids genuinely inserted. An empty list means every one
    was already recorded, which is the ordinary result of a second run — not an
    error, and the thing that makes this safe to re-run after a partial failure.
    """
    return [row["canonical_id"] for row in await conn.fetch(BACKFILL_SQL, user_id)]


async def record_notified(conn, user_id: Any, canonical_ids: Iterable[str]) -> int:
    """Record that this user has now been emailed about these jobs.

    Returns how many rows were genuinely inserted, so a caller can tell a real
    send from a re-send. Not wrapped in its own transaction: the only caller is
    notify/store.record_sent, which already holds one spanning this write, the
    boolean flip and the sent-log receipt.
    """
    ids = list(canonical_ids)
    if not ids:
        return 0

    inserted = 0
    for chunk in _chunks(ids):
        rows = await conn.fetch(record_notified_sql(len(chunk)), user_id, *chunk)
        inserted += len(rows)
    return inserted


async def count_notified_for_user(conn, user_id: Any) -> int:
    """How many jobs this user has been emailed about."""
    return int(await conn.fetchval(COUNT_MINE_SQL, user_id))


async def count_jobs_flagged_notified(conn) -> int:
    """How many jobs carry the old global boolean. The backfill's source of truth."""
    return int(await conn.fetchval(COUNT_NOTIFIED_JOBS_SQL))


async def count_uncovered(conn, user_id: Any) -> int:
    """Jobs marked notified with no row for this user. Anything but 0 is a re-spam.

    The gate, as a query rather than as arithmetic — see UNCOVERED_NOTIFIED_SQL
    for why the count equality alone would not be enough.
    """
    return int(await conn.fetchval(UNCOVERED_NOTIFIED_SQL, user_id))
