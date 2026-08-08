"""Clear the seeded keywords off accounts that never finished onboarding.

The manual half of Phase 3 Slice 1b, and it exists because a seeding fix reaches
only the accounts created after it. dashboard/src/lib/link-user.ts now seeds a
new account with NO keywords; the accounts already sitting in public.users were
seeded with all 43 of this install's defaults, which is somebody else's filter
AND — being over KEYWORD_CAP = 25 — one the preferences screen refuses to save.
Since that save is the only writer of onboarding_completed, such an account
cannot finish onboarding without first deleting eighteen terms it never typed.

WHAT IT TOUCHES, AND WHY THE GUARD IS TWO CLAUSES RATHER THAN ONE.

    WHERE onboarding_completed = FALSE      never chose a filter, so what is on
                                            the row is a seed and not an answer
      AND keywords = <the exact 43>         and it is UNTOUCHED — byte for byte,
                                            in order, the list the seed wrote

Either clause alone would be wrong in a way nobody would notice. On its own the
first would clear a list somebody had typed and not yet saved past the cap —
their work, deleted by a migration. On its own the second would clear the filter
of an account that had deliberately kept the defaults and completed onboarding.
Together they select exactly "a row nobody has ever edited, on an account that
has never been through the screen", which is the only row this is entitled to.

ORDERED EQUALITY, NOT SET EQUALITY, and that is deliberately the stricter test.
`keywords = $1::text[]` compares position by position, so it matches only a list
written in the seed's own order — a person who removed a term and added it back
has an edited row and this leaves it alone. Being too narrow here costs one
account eighteen clicks; being too wide costs somebody their filter.

THE DEFAULTS COME FROM notify.match.DEFAULT_KEYWORDS, never from a literal typed
into this file. That constant is the one dashboard/src/lib/labels.ts mirrors and
tests/test_default_preferences.py compares, so the list this matches on is the
same list the seed wrote by construction rather than by somebody keeping two
copies in step.

WHAT IT DELIBERATELY DOES NOT TOUCH. `updated_at` — the preferences screen shows
it as "last saved", and stamping it would claim an edit the person did not make.
Also cities, exclusions, max_level and every Phase-3 column: this clears the one
list the seed had no business supplying, and nothing else. And it does not set
onboarding_completed either way; the whole point is that the person does that
themselves, on the screen, which they can now reach.

IDEMPOTENT. A second run matches nothing, because the first run left the row
with an empty keywords list that no longer equals the 43.

    python scripts/reset_unonboarded_keywords.py            # preview, writes nothing
    python scripts/reset_unonboarded_keywords.py --apply    # do it
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Allow `python scripts/reset_unonboarded_keywords.py` without installing the
# project as a package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.connection import get_crawler_conn  # noqa: E402
from notify.match import DEFAULT_KEYWORDS  # noqa: E402

# The preview and the write share one predicate, spelled once. Two statements
# whose WHERE clauses are copies of each other is how a preview stops describing
# the thing it previews.
WHERE = """
 WHERE p.onboarding_completed = FALSE
   AND p.keywords = $1::text[]
"""

PREVIEW_SQL = f"""
SELECT p.user_id, u.email, cardinality(p.keywords) AS keyword_count, p.updated_at
  FROM preferences p
  JOIN public.users u ON u.id = p.user_id
{WHERE}
 ORDER BY u.created_at
"""

# RETURNING yields a row only on a genuine write — the idiom
# scripts/backfill_onboarding_prefs.py and crawler/discover.TRACK_SQL both use,
# so the count reported is what changed rather than what was attempted.
RESET_SQL = f"""
UPDATE preferences p
   SET keywords = '{{}}'::text[]
{WHERE}
RETURNING p.user_id
"""

# What must be true of everybody else afterwards, asked of the database rather
# than assumed from the row count above.
VERIFY_SQL = """
SELECT u.email,
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


async def main(apply: bool) -> int:
    keywords = list(DEFAULT_KEYWORDS)
    print(f"The seeded list this matches on: {len(keywords)} keyword(s), in order.")
    print("Connecting to Supabase via CRAWLER_DB_URL (session pooler)...")

    conn = await get_crawler_conn()
    try:
        targets = await conn.fetch(PREVIEW_SQL, keywords)

        print(f"\nRows matching BOTH guard clauses: {len(targets)}")
        for row in targets:
            print(
                f"  user {row['user_id']}  {row['keyword_count']} keyword(s)  "
                f"updated_at {row['updated_at']}"
            )

        if not targets:
            print(
                "\nNothing to do. Either no account is mid-onboarding, or the ones "
                "that are have edited their keywords — in which case the list is "
                "theirs and this script must not touch it."
            )

        written = []
        if apply and targets:
            written = await conn.fetch(RESET_SQL, keywords)

        rows = await conn.fetch(VERIFY_SQL)
    finally:
        await conn.close()

    if apply:
        print(f"\nCleared the seeded keywords on {len(written)} row(s).")
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

    print("\nEvery preferences row, as it now stands:")
    for row in rows:
        print(
            f"  onboarded={str(row['onboarding_completed']):5}  "
            f"keywords={row['keyword_count']:<3} cities={row['city_count']} "
            f"exclusions={row['exclusion_count']} max_level={row['max_level']}  "
            f"updated_at {row['updated_at']}"
        )

    # The blast radius, stated as an assertion rather than left to the reader of
    # the table above. An account that HAS completed onboarding is out of scope by
    # the first guard clause, so an empty keyword list on one of those would mean
    # the guard did not hold.
    broken = [
        r for r in rows if r["onboarding_completed"] and r["keyword_count"] == 0
    ]
    if broken:
        print(
            f"\nFAILED: {len(broken)} onboarded account(s) have an empty keyword "
            f"list. This script must never reach one — an empty list matches no "
            f"job at all, so their digest would go silently quiet.",
            file=sys.stderr,
        )
        return 1

    if apply:
        print(
            "\nDone. Those accounts now start from nothing on /onboarding, save one "
            "keyword of their own, and the gate lets them through."
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
