"""Per-user notification state: who has been emailed about which job.

A ROW EXISTING IS THE DECISION
------------------------------
`user_job_notifications` holds one row per (user, job) pair that has been
emailed. There is no flag on it and no state in between: the row is there, or it
is not. Every statement in this module is written against that reading, which is
why none of them selects `notified_at` — that column is record-keeping (see the
table's comment block in db/schema.sql) and nothing branches on it.

WHY THE SQL LIVES HERE RATHER THAN IN ITS CALLERS
-------------------------------------------------
The same discipline db/watchlist.py follows and tests/test_applied_column.py
established: the statement the tests exercise has to be the statement production
runs, not a paraphrase of it. That mattered most for Phase 1d's backfill — the
single most dangerous statement in the phase, since getting it wrong re-emails a
person's entire job history in one send — and it still holds for what is left.

The digest's own queue read is NOT here. It lives in notify/store.CANDIDATES_SQL,
because it is one statement joining jobs to companies and splitting it across two
modules to isolate four lines of predicate would make the query harder to read
than the coupling it removed. What lives here is everything that WRITES this
table, and nothing else.

The two migrations that filled it are gone as of Phase 1g. Both read
jobs.notified — the 1d backfill to record everything the old boolean covered, and
the 1e reconstruction to gate the stamping of jobs.seeded against it — so they
could not outlive the column, and they had already done their work against the
live database. Git history has them if a future migration needs to see how they
were gated; the shape worth remembering is that both refused and rolled back
rather than trusting their own derivation.

TWO KINDS OF ROW, AND NEITHER IS A LIE
--------------------------------------
Most rows here are receipts: notify/store.record_sent wrote them after Resend
returned an id. The rest were written because the job must NEVER be emailed to
that person — the Phase-1d backfill of everything the old global boolean
covered, and (since 1e) SUPPRESS_COMPANY_HISTORY_SQL, which records a company's
existing jobs the moment somebody starts tracking it. Both kinds say the same thing to the queue,
which is the only thing that reads them: this person will not be told about this
job. What they do not share is a `notifications` receipt, and that is honest —
one was sent, the other never will be.

The silent seed is deliberately NOT handled by writing rows here. It is a
property of a crawl rather than of a person, so it lives in jobs.seeded and the
queue reads it directly; see db/schema.sql for why that is the shape that let 1g
drop the old boolean without changing behaviour. Handling it here instead would
have meant one row per user per seeded job — 6,496 of them from a single
afternoon's discovery sweep, times everyone who ever signs up.

SQL portability note (same as crawler/store.py and notify/store.py): these stay
inside the subset Postgres and SQLite agree on, so the tests run them against the
in-memory shim rather than against a mock.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

# SQLite caps host parameters at 999 and Postgres at 65535; chunk well below both,
# exactly as crawler/store.py does. This is now the only such constant in the
# project that is load bearing: record_notified can be handed every match in a
# digest at once, and the INSERT ... SELECT below binds no list at all.
CHUNK = 400

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
# is the same shape the Phase-1d backfill's rows had, and notified_at takes its
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

# "How many jobs has this person been told about?" Scoped to one user on
# purpose — a table-wide count stopped meaning anything the moment a second
# account could exist, which is the direction this phase travelled in.
COUNT_MINE_SQL = "SELECT count(*) FROM user_job_notifications WHERE user_id = $1"

def _chunks(items: Sequence[str]) -> Iterable[Sequence[str]]:
    for start in range(0, len(items), CHUNK):
        yield items[start : start + CHUNK]



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




async def count_notified_for_user(conn, user_id: Any) -> int:
    """How many jobs this user has been emailed about."""
    return int(await conn.fetchval(COUNT_MINE_SQL, user_id))
