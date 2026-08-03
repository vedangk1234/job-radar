"""Record, per user, every job the global boolean already said was emailed.

    python scripts/backfill_user_notifications.py

THIS IS THE MOST DANGEROUS STEP IN PHASE 1. READ THIS BEFORE RUNNING IT.
------------------------------------------------------------------------
Phase 1d moves "has this person been told about this job" out of the global
jobs.notified boolean and into user_job_notifications, one row per (user, job).
The digest's queue now asks that table.

Which means that until this script has run, that table is EMPTY — and an empty
table says, truthfully and catastrophically, that nobody has ever been told about
anything. The next digest would then consider every open matching job fresh and
email the lot: roughly 7,700 rows of job history, most of it stored by the silent
seed and never intended for an inbox, arriving in one send. That is not a bad
morning, it is the end of the tool — an inbox that has once shown you eighty jobs
you already saw is an inbox you stop opening, which is the exact failure every
guard in this codebase is biased against.

So: APPLY db/schema.sql AND RUN THIS AS ONE UNIT, with no crawl and no digest
between them. The same rule scripts/backfill_preferences_user.py carries, and
here it is the whole point rather than a tidiness argument.

WHY THIS IS A SCRIPT AND NOT SQL IN schema.sql
-----------------------------------------------
The same reason scripts/seed_user.py, backfill_preferences_user.py and
backfill_user_companies.py are: the user is a UUID resolved from MY_EMAIL at
runtime. schema.sql could reach it only by hardcoding a real address — which
scripts/githooks/pre-commit blocks outright — or by guessing, and the guess
available in pure SQL is

    SELECT id FROM public.users LIMIT 1

which is right today by accident and silently wrong the moment there are two
rows. Here that silence would be the worst version of itself: it would record my
entire job history against somebody else's account, leaving both of us wrong —
they can never be told about those jobs again, and I get told about all of them
tomorrow.

IDEMPOTENT, and the guard is the table's own UNIQUE (user_id, canonical_id) via
ON CONFLICT DO NOTHING rather than a check in Python. A second run inserts
nothing and reports so. That is deliberate and load-bearing: it means a run that
dies halfway, or one that fails the gate below because the catalog moved under
it, is fixed by running it again rather than by reasoning about what it got to.

WHY THE GATE IS STRICTER HERE THAN IN backfill_user_companies.py
----------------------------------------------------------------
That script gates on coverage of a snapshot taken before its insert, because a
company appearing mid-run (the discovery sweep, add_workday.py) is not that
migration's business and failing on it would fail a migration that did its job.

The opposite is true here. A job that becomes notified = TRUE while this runs —
the hourly cron seeding a newly-tracked company — is EXACTLY the row that gets
re-emailed if it is left uncovered. There is no such thing as a notified job this
migration is not answerable for. So the gate is absolute, and a mid-run arrival
fails it on purpose. The remedy is to run this again; it is idempotent, so that
costs nothing. Prefer not to start it at the top of the hour.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Allow `python scripts/backfill_user_notifications.py` without installing the
# project as a package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.connection import get_crawler_conn  # noqa: E402
from db.notifications import (  # noqa: E402
    backfill_from_notified,
    count_jobs_flagged_notified,
    count_notified_for_user,
    count_uncovered,
)
from db.users import resolve_current_user_id  # noqa: E402


async def main() -> int:
    print("Connecting to Supabase via CRAWLER_DB_URL (session pooler)...")
    conn = await get_crawler_conn()
    try:
        # Raises with a message naming scripts/seed_user.py if MY_EMAIL is unset
        # or matches no public.users row. Resolving BEFORE any write means a
        # misconfigured environment changes nothing at all — which matters more
        # here than anywhere else, because the wrong id would not fail, it would
        # succeed against the wrong person.
        user_id = await resolve_current_user_id(conn)

        before = await count_jobs_flagged_notified(conn)
        print(f"jobs currently flagged notified = TRUE: {before}")

        recorded = await backfill_from_notified(conn, user_id)

        # Re-read everything from the table rather than inferring it from what
        # the INSERT returned. A rolled-back write, a statement that ran against
        # the wrong user, or a conflict clause that swallowed more than it should
        # all look identical in the return value and different here.
        mine = await count_notified_for_user(conn, user_id)
        flagged = await count_jobs_flagged_notified(conn)
        uncovered = await count_uncovered(conn, user_id)
    finally:
        await conn.close()

    if recorded:
        print(f"Recorded {len(recorded)} notification row(s) for user {user_id}.")
    else:
        print("Nothing to record — every notified job already has a row for this user.")

    print()
    print(f"  notification rows for me : {mine}")
    print(f"  jobs where notified=TRUE : {flagged}")
    print(f"  notified but uncovered   : {uncovered}   <- must be 0")

    # THE GATE, HALF ONE — the safety property. Every job this counts is a job
    # the next digest would treat as fresh and re-send. It is checked first and
    # reported first because it is the one that protects the inbox; the counts
    # below are the arithmetic that explains it.
    if uncovered:
        print(
            f"\nFAILED: {uncovered} job(s) are flagged notified = TRUE but have no "
            f"user_job_notifications row for this user.\n"
            f"        A digest run in this state would re-email every one of them.\n"
            f"        Do NOT run scripts/send_digest.py. Re-run this script — it is "
            f"idempotent — and if the number does not go to 0, stop and investigate "
            f"before any digest runs.",
            file=sys.stderr,
        )
        return 1

    # THE GATE, HALF TWO — the count equality, which is not redundant with the
    # check above. That one proves no notified job is missing; this one proves
    # nothing EXTRA was recorded either. Rows for me that no notified job
    # justifies would mean this migration invented notification state, and the
    # cost of that is a job silently never emailed — the failure that leaves no
    # trace anywhere.
    if mine != flagged:
        print(
            f"\nFAILED: {mine} notification row(s) for this user against {flagged} "
            f"job(s) flagged notified = TRUE. The counts must match exactly.\n"
            f"        More rows than flags means notification state was invented and "
            f"jobs will be silently withheld; fewer means the check above missed "
            f"something. Either way, stop and investigate before any digest runs.",
            file=sys.stderr,
        )
        return 1

    if flagged != before:
        # Not a failure — both gates passed, so every notified job IS covered,
        # including the ones that arrived mid-run. Named because a moving count
        # is worth seeing rather than smoothing over: it means a crawl was
        # running, which the manual steps ask you to avoid.
        print(
            f"\nNOTE: the notified count moved from {before} to {flagged} while this "
            f"ran — a crawl seeded rows underneath it. Both gates still pass, so all "
            f"{flagged} are covered."
        )

    print(
        f"\nSUCCESS: all {flagged} job(s) flagged notified = TRUE are recorded against "
        f"this user, and nothing else is.\n"
        f"         A digest run now considers exactly what it considered before this "
        f"migration."
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception as exc:  # noqa: BLE001 - top-level CLI error reporting
        print(f"\nFAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
