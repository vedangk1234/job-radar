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
that person — the Phase-1d backfill of everything the old global boolean covered,
and (since the filter-first slice) notify/store.suppress_filter_backlog, which
records what a new or widened FILTER has just admitted. Both kinds say the same
thing to the queue, which is the only thing that reads them: this person will not
be told about this job. What they do not share is a `notifications` receipt, and
that is honest — one was sent, the other never will be.

The second kind used to be written on a different trigger — SUPPRESS_COMPANY_
HISTORY_SQL, when somebody started tracking a company — and that statement is
gone with the watch-list gate. See the block where it stood for why the trigger
moved and why it was deleted rather than parked.

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


# THE OTHER HALF OF THE HISTORY HAZARD LIVED HERE UNTIL THE FILTER-FIRST SLICE,
# AS SUPPRESS_COMPANY_HISTORY_SQL, AND IT IS GONE RATHER THAN LEFT UNUSED.
#
# What it did: the moment a (user, company) pair was created, every job stored for
# that company was recorded here as already-notified. "You start hearing about a
# company when you add it, not from its archive." It existed because the digest
# queue opened with `EXISTS (user_companies …)`, so creating a pair admitted that
# company's whole stored history to somebody's queue in one instant — hundreds of
# perfectly ordinary rows, not seeded, never emailed to anybody.
#
# WHY IT WENT. The queue does not read user_companies at all now, so a pair admits
# nothing, and suppressing on the strength of one WITHHOLDS jobs a person is
# already owed. db/watchlist.track_companies spells out the sequence; the short
# form is that pinning a company you already match would silently eat whatever
# arrived at it since your last save, leaving a row claiming the withholding was
# meant. The hazard itself is undiminished — a filter still admits history the
# instant it exists or widens — but its TRIGGER moved from tracking a company to
# changing a filter, which is where the match set is now decided. The replacement
# is notify/store.suppress_filter_backlog, and it reuses RECORD_NOTIFIED_TEMPLATE
# above rather than needing a statement of its own.
#
# DELETED RATHER THAN KEPT AGAINST THE DAY THE WATCH LIST BECOMES A FILTER
# DIMENSION AGAIN, on this codebase's own standing argument: a statement written
# by nobody and read by nobody is worse than absent, because the next person to
# find it reasonably assumes something depends on it. That is why Phase 1e deleted
# db/watchlist.TRACK_ALL_COMPANIES_SQL and why 1g dropped jobs.notified rather
# than leaving either as a net. The git history has the fifteen lines, and
# db/watchlist.track_companies names what would have to come back with them.
#
# ONE PROPERTY OF IT IS WORTH CARRYING FORWARD IN PROSE, because the replacement
# had to re-earn it and could easily not have. It covered EVERY job of the
# company, not only the open ones: a closed job that reopens gets status='open'
# and miss_count=0 back from crawler/store.UPSERT_SEEN while keeping its identity,
# so an open-only suppression leaves a hole exactly one flap wide and `reappeared`
# is the set the crawler most deliberately refuses to treat as news.
# notify/store.SUPPRESSION_CANDIDATES_SQL is status-blind for that reason and for
# no other.


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


async def count_notified_for_user(conn, user_id: Any) -> int:
    """How many jobs this user has been emailed about."""
    return int(await conn.fetchval(COUNT_MINE_SQL, user_id))
