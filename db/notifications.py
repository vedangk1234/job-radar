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
table, plus the statements the two backfills' gates are built from.

TWO KINDS OF ROW, AND NEITHER IS A LIE
--------------------------------------
Most rows here are receipts: notify/store.record_sent wrote them after Resend
returned an id. The rest were written because the job must NEVER be emailed to
that person — the 1d backfill of everything the old boolean covered, and (since
1e) SUPPRESS_COMPANY_HISTORY_SQL, which records a company's existing jobs the
moment somebody starts tracking it. Both kinds say the same thing to the queue,
which is the only thing that reads them: this person will not be told about this
job. What they do not share is a `notifications` receipt, and that is honest —
one was sent, the other never will be.

The silent seed is deliberately NOT handled by writing rows here. It is a
property of a crawl rather than of a person, so it lives in jobs.seeded and the
queue reads it directly; see db/schema.sql for why that is the shape that lets
1g drop the old boolean without changing behaviour.

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
# INSERT ... SELECT rather than a Python loop: the set is decided by the database
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
# No `WHERE TRUE` disambiguator is needed here, and it is worth knowing why the
# question comes up: SQLite cannot parse INSERT ... SELECT ... ON CONFLICT when
# the SELECT has no WHERE clause — it reads `ON` as beginning a join — and
# documents `WHERE true` as the fix. This statement has a real predicate in that
# position, so it parses in both engines and the tests run it character for
# character.
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


# THE OTHER HALF OF THE HISTORY HAZARD — a person, not a crawl.
#
# jobs.seeded keeps a company's FIRST-EVER crawl out of everyone's digest. It
# does nothing about the other way history reaches a new inbox: a user starts
# tracking a company that has been in the catalog for weeks, whose recent
# postings are perfectly ordinary rows — not seeded, never emailed to anyone,
# sitting open. Nothing about those rows is special, and that is the problem:
# there can be hundreds, and they would all be that person's first digest.
#
# So the moment a (user, company) pair is created, every job stored for that
# company becomes that user's history and is recorded here. "You start hearing
# about a company when you add it, not from its archive."
#
# THE ROWS THIS WRITES MEAN WHAT EVERY ROW HERE MEANS: this person will never be
# emailed about this job. They carry no receipt because no email was sent, which
# is the same shape the 1d backfill's rows have, and notified_at takes its
# DEFAULT for the same reason — it records when the state was written, nothing
# branches on it, and inventing a send date would be the mistake source_posted_at
# exists to name.
#
# EVERY job of that company, not just the open ones. A closed job that reopens
# gets status='open' and miss_count=0 back from crawler/store.UPSERT_SEEN while
# keeping its identity — so scoping this to open rows would leave a hole that
# takes exactly one flap to walk through, and `reappeared` is precisely the set
# the crawler refuses to treat as news.
#
# INSERT ... SELECT rather than a Python loop, the same argument BACKFILL_SQL
# above makes: the set is decided by the database at the instant the statement
# runs, so there is no window between "which jobs does this company have" and
# "record them" for the two answers to disagree. The
# caller runs it in the same transaction as the tracking row itself, which closes
# the same window one level up.
SUPPRESS_COMPANY_HISTORY_SQL = """
INSERT INTO user_job_notifications (user_id, canonical_id)
SELECT $1, j.canonical_id
  FROM jobs j
 WHERE j.company_id = $2
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

# THE 1e GATE: "would stamping seeded take anything out of anybody's queue?"
#
# Every (user, job) pair where the job is back catalogue, the user watches its
# company, and nothing here says they have been told. Since 1e the queue reads
# `j.seeded = FALSE`, so each such pair is a job that WAS in that user's digest
# queue and now is not — which for a genuine seed is the entire intention, and
# for a mis-stamped row is a job silently deleted from someone's inbox with no
# trace anywhere.
#
# scripts/backfill_seeded_flag.py runs this before and after its UPDATE and
# refuses to keep the change unless the number is unmoved. That is what makes the
# historical reconstruction safe INDEPENDENTLY of whether its derivation is
# right: the rows it stamps are already covered per-user by the 1d backfill, so
# a wrong guess about which of them were seeded cannot change what anyone is
# sent. The derivation and the migration stop depending on each other.
#
# Joined across every user rather than scoped to one, unlike the gates above:
# this asks about the fan-out as a whole, and a second account is exactly the
# case it has to catch.
SEEDED_BUT_UNCOVERED_SQL = """
SELECT count(*)
  FROM jobs j
  JOIN user_companies uc ON uc.company_id = j.company_id
 WHERE j.seeded = TRUE
   AND NOT EXISTS (
         SELECT 1
           FROM user_job_notifications n
          WHERE n.user_id = uc.user_id
            AND n.canonical_id = j.canonical_id
       )
"""

COUNT_SEEDED_JOBS_SQL = "SELECT count(*) FROM jobs WHERE seeded = TRUE"


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


async def suppress_company_history(conn, user_id: Any, company_id: int) -> list[str]:
    """Record this company's stored jobs as already-notified for this user.

    Returns the canonical_ids genuinely inserted. Called when a (user, company)
    pair is created — see db/watchlist.track_companies, which runs it in the same
    transaction as the tracking row, so there is no instant in which the pair
    exists and its history does not.

    Not wrapped in a transaction here for that reason: the caller holds one, and
    a nested one would claim a guarantee this function cannot make on its own.

    An empty list means every job of that company was already recorded for this
    user — the ordinary result of untracking and re-tracking, which is exactly
    the case that must not re-flood an inbox.
    """
    rows = await conn.fetch(SUPPRESS_COMPANY_HISTORY_SQL, user_id, company_id)
    return [row["canonical_id"] for row in rows]


async def count_seeded_uncovered(conn) -> int:
    """Seeded jobs a watching user has no notification row for. The 1e gate.

    Since the queue reads `seeded = FALSE`, every pair this counts is a job that
    left someone's queue without an email. For a real silent seed that is the
    whole point; for a mis-stamped row it is a job silently withheld. So the
    number is not required to be zero — it is required not to MOVE when the
    historical backfill runs. See SEEDED_BUT_UNCOVERED_SQL.
    """
    return int(await conn.fetchval(SEEDED_BUT_UNCOVERED_SQL))


async def count_seeded_jobs(conn) -> int:
    """How many jobs are marked as back catalogue."""
    return int(await conn.fetchval(COUNT_SEEDED_JOBS_SQL))


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
