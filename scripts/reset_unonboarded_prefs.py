"""Clear the rest of the seeded operator filter off accounts that never onboarded.

The manual half of Phase 3 Slice 1c, and the sibling of
scripts/reset_unonboarded_keywords.py — same shape, same discipline, one slice
later. That script cleared the seeded KEYWORDS when Slice 1b stopped seeding
them. Slice 1c stopped seeding the rest, so this clears the rest.

A seeding fix reaches only the accounts created after it.
dashboard/src/lib/link-user.ts now seeds a new account from a NEW_ACCOUNT_SEED
that is blank on every field; the account already sitting mid-onboarding was
created when the seed still supplied this install's cities and exclusions. Those
are the operator's answers to "where do you want to work" and "what should never
reach you", sitting on somebody else's row looking exactly like decisions they
made.

WHAT IT SETS, AND WHAT IT DELIBERATELY LEAVES ALONE.

    cities      -> '{}'     the seed had no business supplying a location
    exclusions  -> '{}'     nor a list of titles to drop
    max_level   -> UNTOUCHED

max_level IS IN THE GUARD BUT NOT IN THE UPDATE, and that is the one asymmetry
in this file. NEW_ACCOUNT_SEED does not state a max_level at all: the column is
INT NOT NULL DEFAULT 2, so NULL cannot mean "unset" and branch 3 reaches the
blank by omitting the column and letting the schema default stand. A genuinely
new account therefore HAS max_level = 2 — verified by npm run onboarding-check
against a live throwaway account — which is what this row already holds. Writing
anything here would move it AWAY from what a fresh signup gets. It is matched on
so the guard can still prove the row is unedited, and then left exactly as it is.

NOTHING ELSE ON THE ROW NEEDS CLEARING, and that was checked rather than
assumed. The nine columns Phase 3 Step 0 added are already at their blank
defaults on this account — empty arrays, FALSE booleans, NULL ints — because
they were added after it was created and branch 3 has never named them. The
operator's row is the one carrying values in them, and the operator is out of
scope. So this file does not mention those columns: a statement that clears a
column already blank is a statement that can only ever do harm.

WHY THE GUARD IS TWO CLAUSES RATHER THAN ONE, AND WHY IT MATTERS MORE HERE.

     WHERE onboarding_completed = FALSE     never chose a filter, so what is on
                                            the row is a seed and not an answer
       AND cities     = <the exact 3>       and every field is UNTOUCHED — byte
       AND exclusions = <the exact 14>      for byte, in order, the values the
       AND max_level  = <the default>       seed wrote

Either clause alone is wrong in a way nobody would notice, and on THIS database
the second one is demonstrably wrong: the operator's row also carries exactly
these three values, because they are the install's defaults and the operator
kept them. So clause 2 on its own would match the account that has been running
for months and blank the filter its digest depends on — an empty cities list
matches no job at all, and the symptom is a quiet inbox rather than an error.
Clause 1 on its own would clear values somebody had typed into the onboarding
screen and not yet saved. The preview below reports what each clause would match
in isolation, so that argument is a number on the screen rather than a paragraph.

ORDERED EQUALITY, NOT SET EQUALITY, exactly as the 1b script argues. Comparing
`cities = $1::text[]` is position by position, so a row whose list was reordered
by a person is an EDITED row and this leaves it alone. Being too narrow costs one
account a few clicks on a screen it is already sitting on; being too wide costs
somebody the filter their digest runs on.

THE VALUES COME FROM notify.match, never from literals typed into this file.
DEFAULT_CITIES / DEFAULT_EXCLUSIONS / DEFAULT_MAX_LEVEL are the constants
dashboard/src/lib/labels.DEFAULT_PREFERENCES mirrors and
tests/test_default_preferences.py compares against, so the values matched here
are the values the seed wrote by construction rather than by somebody keeping a
third copy in step.

WHAT IT DOES NOT TOUCH. `updated_at` — the preferences screen shows it as "last
saved", and stamping it would claim an edit the person did not make. And it does
not set onboarding_completed either way: the whole point is that the person does
that themselves, on the screen, by saving answers of their own.

IDEMPOTENT. A second run matches nothing, because the first left the row with
empty cities and exclusions that no longer equal the seeded lists.

    python scripts/reset_unonboarded_prefs.py            # preview, writes nothing
    python scripts/reset_unonboarded_prefs.py --apply    # do it
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Allow `python scripts/reset_unonboarded_prefs.py` without installing the
# project as a package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.connection import get_crawler_conn  # noqa: E402
from notify.match import (  # noqa: E402
    DEFAULT_CITIES,
    DEFAULT_EXCLUSIONS,
    DEFAULT_MAX_LEVEL,
)

# The preview and the write share one predicate, spelled once. Two statements
# whose WHERE clauses are copies of each other is how a preview stops describing
# the thing it previews.
WHERE = """
 WHERE p.onboarding_completed = FALSE
   AND p.cities     = $1::text[]
   AND p.exclusions = $2::text[]
   AND p.max_level  = $3
"""

PREVIEW_SQL = f"""
SELECT p.user_id,
       u.email,
       p.cities,
       cardinality(p.exclusions) AS exclusion_count,
       p.max_level,
       p.updated_at
  FROM preferences p
  JOIN public.users u ON u.id = p.user_id
{WHERE}
 ORDER BY u.created_at
"""

# EACH CLAUSE IN ISOLATION, so "the guard is what excludes the operator" is a
# number rather than a claim. On this database the second query returns MORE rows
# than the first — the operator kept the install's defaults, so the value test
# alone selects the one account this script must never reach.
GATE_ONLY_SQL = """
SELECT count(*)::int AS n FROM preferences WHERE onboarding_completed = FALSE
"""

VALUES_ONLY_SQL = """
SELECT count(*)::int AS n
  FROM preferences
 WHERE cities = $1::text[] AND exclusions = $2::text[] AND max_level = $3
"""

# RETURNING yields a row only on a genuine write — the idiom
# scripts/reset_unonboarded_keywords.py and crawler/discover.TRACK_SQL both use,
# so the count reported is what changed rather than what was attempted.
#
# max_level is absent from this SET on purpose; see the module docstring.
RESET_SQL = f"""
UPDATE preferences p
   SET cities     = '{{}}'::text[],
       exclusions = '{{}}'::text[]
{WHERE}
RETURNING p.user_id
"""

# What must be true of everybody afterwards, asked of the database rather than
# assumed from the row count above.
VERIFY_SQL = """
SELECT u.id,
       p.onboarding_completed,
       cardinality(p.keywords)   AS keyword_count,
       cardinality(p.cities)     AS city_count,
       cardinality(p.exclusions) AS exclusion_count,
       p.max_level,
       p.updated_at
  FROM preferences p
  JOIN public.users u ON u.id = p.user_id
 ORDER BY u.created_at
"""


def _row_line(row) -> str:
    return (
        f"  {str(row['id'])[:8]}  onboarded={str(row['onboarding_completed']):5}  "
        f"keywords={row['keyword_count']:<3} cities={row['city_count']} "
        f"exclusions={row['exclusion_count']:<3} max_level={row['max_level']}  "
        f"updated_at {row['updated_at']}"
    )


async def main(apply: bool) -> int:
    cities = list(DEFAULT_CITIES)
    exclusions = list(DEFAULT_EXCLUSIONS)
    max_level = DEFAULT_MAX_LEVEL

    print(
        f"The seeded values this matches on: cities {cities}, "
        f"{len(exclusions)} exclusion(s) in order, max_level {max_level}."
    )
    print("It clears cities and exclusions. max_level is matched and LEFT ALONE —")
    print("a genuinely new account has max_level 2, so writing here would move it away.")
    print("\nConnecting to Supabase via CRAWLER_DB_URL (session pooler)...")

    conn = await get_crawler_conn()
    try:
        before = await conn.fetch(VERIFY_SQL)

        print("\nEvery preferences row, BEFORE:")
        for row in before:
            print(_row_line(row))

        targets = await conn.fetch(PREVIEW_SQL, cities, exclusions, max_level)

        # The guard, taken apart. Printed before the match list because it is the
        # thing that decides whether the match list is trustworthy.
        gate_only = await conn.fetchval(GATE_ONLY_SQL)
        values_only = await conn.fetchval(VALUES_ONLY_SQL, cities, exclusions, max_level)
        print("\nWhat each guard clause would match ON ITS OWN:")
        print(f"  onboarding_completed = FALSE alone : {gate_only} row(s)")
        print(f"  the three value tests alone        : {values_only} row(s)")
        print(f"  BOTH together (what this uses)     : {len(targets)} row(s)")
        if values_only > len(targets):
            print(
                f"  -> the value tests alone would reach {values_only - len(targets)} "
                f"onboarded account(s), including the operator's. That is what the "
                f"first clause is for."
            )

        print(f"\nRows matching BOTH guard clauses: {len(targets)}")
        for row in targets:
            print(
                f"  user {row['user_id']}  cities={list(row['cities'])}  "
                f"{row['exclusion_count']} exclusion(s)  max_level={row['max_level']}  "
                f"updated_at {row['updated_at']}"
            )

        if not targets:
            print(
                "\nNothing to do. Either no account is mid-onboarding, or the ones "
                "that are have edited these fields — in which case the values are "
                "theirs and this script must not touch them."
            )

        written = []
        if apply and targets:
            written = await conn.fetch(RESET_SQL, cities, exclusions, max_level)

        after = await conn.fetch(VERIFY_SQL)
    finally:
        await conn.close()

    if apply:
        print(f"\nCleared the seeded cities and exclusions on {len(written)} row(s).")
        if len(written) != len(targets):
            print(
                f"\nFAILED: the preview selected {len(targets)} row(s) and the UPDATE "
                f"wrote {len(written)}. Something edited a row between the two "
                f"statements; nothing here is safe to report.",
                file=sys.stderr,
            )
            return 1
    else:
        print("\nPREVIEW ONLY — nothing was written. Re-run with --apply to do it.")

    print("\nEvery preferences row, AFTER:")
    for row in after:
        print(_row_line(row))

    # The blast radius, stated as an assertion rather than left to the reader of
    # the two tables above. An account that HAS completed onboarding is out of
    # scope by the first guard clause, so an empty city or exclusion list on one
    # of those would mean the guard did not hold.
    broken = [r for r in after if r["onboarding_completed"] and r["city_count"] == 0]
    if broken:
        print(
            f"\nFAILED: {len(broken)} onboarded account(s) have an empty city list. "
            f"This script must never reach one — an empty cities list matches no job "
            f"at all, so their digest would go silently quiet.",
            file=sys.stderr,
        )
        return 1

    # updated_at is not stamped by this script, so every row's must be exactly what
    # it was. Compared by user id rather than by position, because ORDER BY
    # created_at would happily return the same rows in the same order while one of
    # them carried a new timestamp.
    was = {r["id"]: r["updated_at"] for r in before}
    moved = [r for r in after if was.get(r["id"]) != r["updated_at"]]
    if moved:
        print(
            f"\nFAILED: updated_at moved on {len(moved)} row(s). This script clears "
            f"values a seed wrote and must not claim an edit anybody made — the "
            f"preferences screen shows that column as 'last saved'.",
            file=sys.stderr,
        )
        return 1

    if apply:
        print(
            "\nDone. That account now meets /onboarding exactly as a brand-new signup "
            "does: no keywords, no cities, no exclusions, max_level at the schema "
            "default, and every answer on the screen still to be given."
        )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="perform the UPDATE. Without it the script only reports what it would touch.",
    )
    raise SystemExit(asyncio.run(main(parser.parse_args().apply)))
