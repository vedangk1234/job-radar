"""Reconstruct jobs.seeded for rows stored before the column existed.

    python scripts/backfill_seeded_flag.py --dry-run
    python scripts/backfill_seeded_flag.py

Phase 1e splits the two facts jobs.notified was carrying. "This row is back
catalogue" moves to jobs.seeded, where the crawl writes it and the digest queue
reads it; "somebody was emailed this" stays in user_job_notifications, where 1d
put it. The ALTER gives every existing row seeded = FALSE, and this script is
what makes the column true about the ~7,700 rows that predate it.

THE DERIVATION, AND WHY IT HOLDS
--------------------------------
    seeded = TRUE  where  notified = TRUE  and no `notifications` receipt exists

Exactly two statements have ever written notified = TRUE. crawler/store.mark_new
does it for a company's whole back catalogue on its first-ever crawl and writes
no receipt — that IS the silent seed. notify/store.record_sent does it after a
send, and writes the receipt in the SAME TRANSACTION, so a sent job cannot lack
one. (Checked against the whole tree and the whole of git history before this was
written: no migration, repair script, relabel pass or dashboard route has ever
touched the column. The dashboard's only write to jobs names applied/applied_at.)
So "flagged, with no receipt" is precisely the seed, and the live numbers agree
with it from the other side: 7,706 flags against 15 receipts, which CLAUDE.md
already recorded as "the gap is the silent seed".

THE GATE, AND WHY IT MATTERS MORE THAN THE DERIVATION
-----------------------------------------------------
Being right about history is not the safety property. The safety property is that
being WRONG about it cannot change what anybody is sent — because the queue reads
seeded = FALSE, so a row stamped in error is a job silently deleted from
someone's digest, the failure that leaves no trace anywhere.

So this script counts, before and after, every (user, job) pair where the job is
seeded, the user watches its company, and nothing records them as notified —
db/notifications.SEEDED_BUT_UNCOVERED_SQL. Each such pair is a job that WAS in
someone's queue and now is not. The gate is that this number does not move. It
holds because the 1d backfill already gave the existing user a row for every
notified job, so every row this stamps is one they were never going to be emailed
about anyway; if that is not true of some row, this refuses and rolls back rather
than discovering it three quiet digests later.

That makes the migration safe INDEPENDENTLY of the derivation being right, which
is the only version worth running against a live inbox.

IDEMPOTENT: the UPDATE skips rows already stamped, so a second run reports zero
and changes nothing. Run --dry-run first; it does the counting and the UPDATE
inside a transaction it always rolls back.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.connection import get_crawler_conn  # noqa: E402
from db.notifications import (  # noqa: E402
    count_jobs_flagged_notified,
    count_seeded_jobs,
    count_seeded_uncovered,
)

# The reconstruction. `seeded = FALSE` in the predicate is what makes a re-run a
# no-op, and RETURNING counts what genuinely changed rather than what was
# attempted — the idiom every backfill in this project uses.
#
# NOT EXISTS against `notifications` rather than a LEFT JOIN, matching the queue's
# own shape: the question is existence, and a job can carry several receipts (one
# per digest it appeared in), which a join would multiply.
BACKFILL_SEEDED_SQL = """
UPDATE jobs
   SET seeded = TRUE
 WHERE notified = TRUE
   AND seeded = FALSE
   AND NOT EXISTS (
         SELECT 1
           FROM notifications n
          WHERE n.canonical_id = jobs.canonical_id
       )
RETURNING canonical_id
"""

# The rows the derivation deliberately leaves alone, reported so the number is
# seen rather than assumed. A job that was seeded AND later emailed carries a
# receipt, so it stays seeded = FALSE — a false negative, and a harmless one: it
# already has a per-user row for everyone it was sent to, and for anybody who
# starts watching that company later, db/watchlist.track_companies records it as
# history at that moment.
FLAGGED_WITH_RECEIPT_SQL = """
SELECT count(*)
  FROM jobs j
 WHERE j.notified = TRUE
   AND EXISTS (
         SELECT 1
           FROM notifications n
          WHERE n.canonical_id = j.canonical_id
       )
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Set jobs.seeded for rows the silent seed stored before the column existed."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="run the UPDATE and the gate inside a transaction, then roll back",
    )
    return parser.parse_args()


async def main() -> int:
    args = parse_args()

    print("Connecting to Supabase via CRAWLER_DB_URL (session pooler)...")
    conn = await get_crawler_conn()
    failed = False
    try:
        flagged = await count_jobs_flagged_notified(conn)
        with_receipt = int(await conn.fetchval(FLAGGED_WITH_RECEIPT_SQL))
        seeded_before = await count_seeded_jobs(conn)

        # THE GATE'S BASELINE. Taken before any write, because the assertion is
        # that this number is unmoved by the UPDATE — an absolute value would
        # only say what the database happened to look like today.
        uncovered_before = await count_seeded_uncovered(conn)

        print(f"jobs flagged notified = TRUE : {flagged}")
        print(f"  ... of which have a receipt: {with_receipt}   <- left alone, see docstring")
        print(f"  ... derived as seeded      : {flagged - with_receipt}")
        print(f"already marked seeded        : {seeded_before}")
        print(f"seeded, watched, uncovered   : {uncovered_before}   <- must not move")

        async with conn.transaction():
            stamped = await conn.fetch(BACKFILL_SEEDED_SQL)
            seeded_after = await count_seeded_jobs(conn)
            uncovered_after = await count_seeded_uncovered(conn)

            print(f"\nstamped this run             : {len(stamped)}")
            print(f"now marked seeded            : {seeded_after}")
            print(f"seeded, watched, uncovered   : {uncovered_after}")

            if uncovered_after != uncovered_before:
                print(
                    f"\nFAILED: stamping moved the uncovered count from "
                    f"{uncovered_before} to {uncovered_after}.\n"
                    f"        {uncovered_after - uncovered_before} job(s) just left a "
                    f"digest queue without anyone having been emailed about them — "
                    f"silently, which is the one failure this project has no other "
                    f"guard against.\n"
                    f"        Rolling back. Do NOT re-run until the rows in question "
                    f"are understood: they are jobs flagged notified = TRUE, with no "
                    f"receipt, that a user watching their company has no "
                    f"user_job_notifications row for. That combination should be "
                    f"impossible after the 1d backfill.",
                    file=sys.stderr,
                )
                failed = True
                raise _Rollback()

            if seeded_after != flagged - with_receipt:
                print(
                    f"\nFAILED: {seeded_after} row(s) marked seeded against "
                    f"{flagged - with_receipt} derived. The two must agree exactly; "
                    f"a gap means something set the flag outside this script, or the "
                    f"notified count moved while it ran.\n"
                    f"        Rolling back.",
                    file=sys.stderr,
                )
                failed = True
                raise _Rollback()

            if args.dry_run:
                print("\nDRY RUN — both gates passed. Rolling back; nothing was kept.")
                raise _Rollback()

    except _Rollback:
        pass
    finally:
        await conn.close()

    if failed:
        return 1
    if args.dry_run:
        print("Re-run without --dry-run to keep the change.")
        return 0

    print(
        f"\nSUCCESS: jobs.seeded now records the silent seed, and no job left "
        f"anybody's digest queue in the process.\n"
        f"         The queue reads this column instead of the old boolean; "
        f"jobs.notified is still written and is dropped in 1g."
    )
    return 0


class _Rollback(Exception):
    """Leave the transaction by raising, so asyncpg rolls it back for us.

    A deliberate exception rather than a flag checked after the `async with`:
    the transaction commits on a clean exit, so "decide not to keep this" has to
    be an abnormal exit or it is not a rollback at all.
    """


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception as exc:  # noqa: BLE001 - top-level CLI error reporting
        print(f"\nFAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
