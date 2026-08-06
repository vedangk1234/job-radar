"""Seed the existing account's Phase-3 columns so its filter keeps behaving.

The second half of the db/schema.sql change that added nine columns to
`preferences`, and it is a script rather than a guarded UPDATE in that file for a
reason that is specific to this migration and worth stating plainly.

WHY NOT AN UPDATE IN db/schema.sql. Every predicate available there identifies
the row by what it CONTAINS — `onboarding_completed = FALSE`, or an empty
`selected_cities`. Those are true of the existing account today and equally true
of every account created from now until the onboarding screen exists, because
dashboard/src/lib/link-user.ts creates a new user's preferences row with all nine
columns at their defaults. schema.sql is re-applied by this very script whenever
anything else in it changes, so such an UPDATE would eventually run against a
stranger's fresh row and mark them as already-onboarded — skipping the screen
they had never seen, with a filter they never chose, silently. There is no
predicate in pure SQL for "the rows that existed before this migration".

So the target is resolved the way scripts/backfill_preferences_user.py resolves
it: MY_EMAIL -> public.users, once, before any write, raising rather than
guessing. A misconfigured environment changes nothing at all.

WHAT THE VALUES REPRODUCE. Today's digest keeps a job when its stated minimum is
at most 2 years and keeps it when nothing is known — see notify/match.py's
MAX_MIN_EXPERIENCE and experience_excluded. So:

    exp_range_low  = 0    the rule keeps min_experience 0, 1 and 2; the low end
                          is 0 rather than 1 because 0 is inside today's answer
    exp_range_high = 2    MAX_MIN_EXPERIENCE, the ceiling itself
    show_no_experience_jobs = TRUE

That last one is the one to read twice. The COLUMN default is FALSE, which is
right for somebody who has not answered the question; today's behaviour is the
opposite — min_experience IS NULL means KEEP, and it covers "never read", "read,
no minimum stated" and "the fetch failed". Leaving this account at FALSE would
mean the feature step that wires the column in silently starts hiding every
unenriched job, which is the expensive failure this project spends its guards on.

    selected_cities = cities   COPIED FROM THE LIVE COLUMN, not written as a
                               literal, so it cannot disagree with what the
                               preferences screen last saved. It lands in today's
                               label vocabulary (blr | pune | hyd | ...); the
                               3->25 city step owns translating it.
    all_india    = FALSE       a three-city list is not all-India
    work_modes   = onsite, hybrid, remote     nothing is filtered on mode today
    salary_bands = every band                nothing is filtered on pay today
    onboarding_completed = TRUE               a configured account, not a fresh one

    years_of_experience_held stays NULL. It is informational, it never filters,
    and the honest thing to say about a number nobody has been asked for is
    nothing. Inventing one is the mistake source_posted_at exists to name.

WHAT IT DELIBERATELY DOES NOT TOUCH. `updated_at`, and `cities` / `keywords` /
`exclusions` / `max_level` — the four columns that actually decide. updated_at
means "when this filter was last edited" and is what the preferences screen
shows; stamping it here would claim an edit that did not happen, for a migration
that changed no filter. Reading the whole row back afterwards is what proves the
four are untouched rather than asserting it.

IDEMPOTENT, guarded on `onboarding_completed = FALSE`. A second run matches
nothing, reports "already seeded", and cannot overwrite an answer given in the
onboarding screen later.

    python scripts/backfill_onboarding_prefs.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Allow `python scripts/backfill_onboarding_prefs.py` without installing the
# project as a package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.connection import get_crawler_conn  # noqa: E402
from db.users import resolve_current_user_id  # noqa: E402

WORK_MODES = ["onsite", "hybrid", "remote"]
SALARY_BANDS = ["<10", "10-15", "15-25", "25-40", "40-50", "50+"]

# RETURNING id yields a row only on a genuine write, the same idiom
# scripts/backfill_preferences_user.py and crawler/discover.TRACK_SQL use to
# count what actually changed rather than what was attempted.
#
# selected_cities = cities is a column reference, not a bound parameter: the
# value is read and written inside one statement, so nothing can edit the filter
# between the read and the write.
BACKFILL_SQL = """
UPDATE preferences
   SET exp_range_low           = 0,
       exp_range_high          = 2,
       show_no_experience_jobs = TRUE,
       selected_cities         = cities,
       all_india               = FALSE,
       work_modes              = $2::text[],
       salary_bands            = $3::text[],
       onboarding_completed    = TRUE
 WHERE user_id = $1
   AND onboarding_completed = FALSE
RETURNING id
"""

# Read back afterwards rather than from RETURNING, so the report describes the
# row as it now stands whether this run wrote it or a previous one did.
VERIFY_SQL = """
SELECT cities, keywords, exclusions, max_level, updated_at,
       exp_range_low, exp_range_high, years_of_experience_held,
       show_no_experience_jobs, selected_cities, all_india,
       work_modes, salary_bands, onboarding_completed
  FROM preferences
 WHERE user_id = $1
"""


async def main() -> int:
    print("Connecting to Supabase via CRAWLER_DB_URL (session pooler)...")
    conn = await get_crawler_conn()
    try:
        # Raises with a message naming scripts/seed_user.py if MY_EMAIL is unset
        # or matches no users row. Resolved BEFORE the UPDATE for that reason.
        user_id = await resolve_current_user_id(conn)

        seeded = await conn.fetch(BACKFILL_SQL, user_id, WORK_MODES, SALARY_BANDS)
        rows = await conn.fetch(VERIFY_SQL, user_id)

        # The blast radius, stated as a number. This migration is meant to touch
        # exactly one account's row and no other.
        others = await conn.fetchval(
            "SELECT count(*) FROM preferences WHERE user_id IS DISTINCT FROM $1 "
            "AND onboarding_completed = TRUE",
            user_id,
        )
    finally:
        await conn.close()

    if seeded:
        print(f"Seeded {len(seeded)} preferences row(s) for user {user_id}.")
    else:
        print("Nothing to seed — this user's row already has onboarding_completed = TRUE.")

    if not rows:
        print(
            f"\nFAILED: no preferences row for user {user_id}. Run "
            f"scripts/backfill_preferences_user.py first.",
            file=sys.stderr,
        )
        return 1

    row = rows[0]

    print("\nThe filter that decides (UNCHANGED by this script):")
    print(f"  cities      {list(row['cities'])}")
    print(f"  keywords    {len(row['keywords'])} term(s)")
    print(f"  exclusions  {len(row['exclusions'])} term(s)")
    print(f"  max_level   {row['max_level']}")
    print(f"  updated_at  {row['updated_at']}")

    print("\nThe Phase-3 columns (read by nothing yet):")
    print(f"  exp_range_low            {row['exp_range_low']}")
    print(f"  exp_range_high           {row['exp_range_high']}")
    print(f"  years_of_experience_held {row['years_of_experience_held']}")
    print(f"  show_no_experience_jobs  {row['show_no_experience_jobs']}")
    print(f"  selected_cities          {list(row['selected_cities'])}")
    print(f"  all_india                {row['all_india']}")
    print(f"  work_modes               {list(row['work_modes'])}")
    print(f"  salary_bands             {list(row['salary_bands'])}")
    print(f"  onboarding_completed     {row['onboarding_completed']}")

    if others:
        print(f"\nNote: {others} other account(s) also have onboarding_completed = TRUE.")

    # The gate. Each of these is a value a later feature step will read as a
    # decision, so a half-seeded row is worse than an unseeded one: it looks
    # answered and is not.
    problems = []
    if not row["onboarding_completed"]:
        problems.append("onboarding_completed is still FALSE")
    if not row["show_no_experience_jobs"]:
        problems.append(
            "show_no_experience_jobs is FALSE, which would hide every job whose "
            "description has not been read"
        )
    if (row["exp_range_low"], row["exp_range_high"]) != (0, 2):
        problems.append(
            f"the looking-range is ({row['exp_range_low']}, {row['exp_range_high']}), "
            f"not (0, 2) — it no longer reproduces today's <=2 rule"
        )
    if not list(row["selected_cities"]):
        problems.append("selected_cities is empty, so no city survived the copy from cities")
    if sorted(row["work_modes"]) != sorted(WORK_MODES):
        problems.append(f"work_modes is {list(row['work_modes'])}, not all three")
    if sorted(row["salary_bands"]) != sorted(SALARY_BANDS):
        problems.append(f"salary_bands is {list(row['salary_bands'])}, not all six")
    if list(row["selected_cities"]) != list(row["cities"]):
        problems.append(
            f"selected_cities {list(row['selected_cities'])} does not equal cities "
            f"{list(row['cities'])} — the filter was edited between the copy and this read"
        )

    if problems:
        print("\nFAILED:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    print("\nSUCCESS: this account's Phase-3 columns reproduce its current filter, "
          "and the four columns that decide are untouched.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception as exc:  # noqa: BLE001 - top-level CLI error reporting
        print(f"\nFAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
