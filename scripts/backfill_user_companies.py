"""Track every company in the catalog for the single existing user.

The second half of the db/schema.sql change that added user_companies, and it is
a script rather than a guarded INSERT in that file for exactly the reason
scripts/seed_user.py and scripts/backfill_preferences_user.py are: the user is a
UUID resolved from MY_EMAIL at runtime. schema.sql could only reach it by
hardcoding a real address — which scripts/githooks/pre-commit blocks outright —
or by guessing, and the guess available in pure SQL is

    SELECT id FROM public.users LIMIT 1

which is right today by accident and silently wrong the moment there are two
rows: it would hand my watch list to somebody else's account without raising.

WHY THIS EXISTS AT ALL: behaviour must not change. Before this table, "my
companies" was every company in the catalog, implicitly, because there was one
user and nothing in the database naming them. This script writes that implicit
answer down, so the 1e fan-out starts from exactly the set the digest already
used rather than from an empty watch list and a silent week.

RUN THIS IMMEDIATELY AFTER APPLYING THE SCHEMA, AS ONE UNIT — the same rule the
preferences backfill carries, though this window is gentler: nothing reads
user_companies yet, so between the CREATE TABLE and this INSERT the only thing
that is wrong is that the table is empty. Nothing crawls differently and no
digest changes. Do it as one unit anyway, because "the migration is half applied"
is not a state worth being able to be in.

Idempotent, and the guard is the table's own UNIQUE (user_id, company_id) via
ON CONFLICT DO NOTHING rather than a check in Python. A second run inserts
nothing, reports "already tracking N companies", and cannot duplicate a row.

SINCE PHASE 1e THIS ALSO SUPPRESSES HISTORY, because creating a tracking row now
has two halves — see db/watchlist.track_companies. For the original user that is
a no-op (the 1d backfill already recorded every notified job against them, and
the rest of the catalog's rows were already in their queue before any of this
existed). For a SECOND user run through this script it is the whole safety story:
fifty companies' stored jobs would otherwise be their first digest. The count is
reported below so that no-op is visible rather than assumed.

    python scripts/backfill_user_companies.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Allow `python scripts/backfill_user_companies.py` without installing the
# project as a package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.connection import get_crawler_conn  # noqa: E402
from db.notifications import count_notified_for_user  # noqa: E402
from db.users import resolve_current_user_id  # noqa: E402
from db.watchlist import (  # noqa: E402
    catalog_company_ids,
    track_all_companies,
    tracked_company_ids,
)


def uncovered(catalog_before: list[int], tracked_after: list[int]) -> list[int]:
    """Catalog ids from the snapshot that are NOT tracked. Empty means success.

    The gate, as a function of the two sets rather than of two counts, which is
    what makes it immune to the catalog growing mid-run: an id that was never in
    the snapshot cannot be missing from it. Extracted so the rule is testable
    without a database — see tests/test_user_companies.py.
    """
    tracked = set(tracked_after)
    return [cid for cid in catalog_before if cid not in tracked]


async def main() -> int:
    print("Connecting to Supabase via CRAWLER_DB_URL (session pooler)...")
    conn = await get_crawler_conn()
    try:
        # Raises with a message naming scripts/seed_user.py if MY_EMAIL is unset
        # or matches no public.users row. Resolving BEFORE any write means a
        # misconfigured environment changes nothing at all.
        user_id = await resolve_current_user_id(conn)

        # THE SNAPSHOT, AND WHY THE GATE IS BUILT ON IT RATHER THAN ON A FRESH
        # COUNT. The obvious success test — re-count the catalog afterwards and
        # require tracked == catalog — is wrong in a way that only shows up
        # rarely and looks like a broken migration when it does. The catalog is
        # not frozen while this runs: scripts/run_discovery.py auto-tracks new
        # boards, and add_workday.py / seed_companies.py insert by hand. A
        # company inserted between the INSERT ... SELECT below and a later
        # re-count is a company this run never saw and was never responsible
        # for, yet it would make the two numbers disagree and fail a migration
        # that did its job perfectly.
        #
        # So the set this run is answerable for is captured here, before the
        # write, and the gate asserts coverage of THAT set — every id in it is
        # genuinely present in user_companies afterwards, read back from the
        # table rather than inferred from arithmetic. A company added mid-run is
        # neither required nor silently ignored: it is reported below and the
        # next run picks it up. (The alternative was REPEATABLE READ around the
        # write and the count, which buys the same guarantee at the cost of an
        # isolation level this codebase uses nowhere else, and still leaves the
        # new company invisible instead of named.)
        catalog_before = await catalog_company_ids(conn)

        suppressed_before = await count_notified_for_user(conn, user_id)
        newly_tracked = await track_all_companies(conn, user_id)
        suppressed_after = await count_notified_for_user(conn, user_id)

        tracked_after = await tracked_company_ids(conn, user_id)
        catalog_after = await catalog_company_ids(conn)
    finally:
        await conn.close()

    already = len(catalog_before) - len(newly_tracked)

    if newly_tracked:
        print(f"Now tracking {len(newly_tracked)} new company/ies for user {user_id}.")
        if already > 0:
            print(f"  ({already} of the {len(catalog_before)} in the catalog were already tracked.)")
    else:
        print(f"Nothing to track — already tracking {len(tracked_after)} companies.")

    print(
        f"catalog: {len(catalog_before)} company/ies at snapshot; "
        f"user_companies: {len(tracked_after)} row(s) for this user."
    )

    # The Phase-1e half. Zero is the expected number for the ORIGINAL user and is
    # printed rather than skipped, because "nothing was suppressed" and "the
    # suppression did not run" look identical in an inbox six hours later.
    suppressed = suppressed_after - suppressed_before
    print(
        f"history suppressed: {suppressed} job(s) recorded as already-notified for "
        f"this user, so newly-tracked companies do not deliver their back catalogue."
    )

    # A company that appeared while this ran. Named rather than counted, because
    # "the catalog grew by one" is a fact about the sweep and not about this
    # migration — and because the fix is simply to run this again.
    appeared = [cid for cid in catalog_after if cid not in set(catalog_before)]
    if appeared:
        print(
            f"\nNOTE: {len(appeared)} company/ies were added to the catalog while this ran "
            f"(ids {', '.join(str(c) for c in appeared)}). They are outside this run's "
            f"snapshot and are not a failure — re-run this script to track them."
        )

    # THE GATE. Coverage of the snapshot, verified against the rows that are
    # actually in the table, so a rolled-back insert or a write against the wrong
    # user fails loudly here instead of surfacing as an empty digest in 1e.
    missing = uncovered(catalog_before, tracked_after)
    if missing:
        print(
            f"\nFAILED: {len(missing)} company/ies from the catalog snapshot are not tracked "
            f"(ids {', '.join(str(c) for c in missing)}).",
            file=sys.stderr,
        )
        return 1

    print(
        f"\nSUCCESS: every one of the {len(catalog_before)} companies in the catalog snapshot "
        f"is tracked by this user."
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception as exc:  # noqa: BLE001 - top-level CLI error reporting
        print(f"\nFAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
