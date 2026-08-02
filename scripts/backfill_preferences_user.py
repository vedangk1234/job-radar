"""Attach the existing preferences row to the seeded user.

The second half of the db/schema.sql change that added preferences.user_id, and
it has to be a script rather than a guarded UPDATE in that file for the same
reason scripts/seed_user.py exists: the value is a UUID resolved from MY_EMAIL
at runtime. schema.sql could only reach it by hardcoding a real address — which
scripts/githooks/pre-commit blocks outright — or by guessing, and the guess
available in pure SQL is

    SET user_id = (SELECT id FROM public.users LIMIT 1)

which is right today by accident and silently wrong the moment there are two
rows. Silently is the operative word: it would attach my filter to somebody
else's account and nothing would raise.

RUN THIS IMMEDIATELY AFTER APPLYING THE SCHEMA, AS ONE UNIT. Between the ALTER
and this UPDATE the preferences row exists but is unowned, and a user-scoped
loader finds nothing for anybody. The loaders detect that state and refuse
rather than quietly matching on defaults (see notify/store.load_preferences),
so nothing runs on the wrong filter — but a crawl or digest in that window is a
crawl or digest that fails, so do not leave one scheduled into it.

Idempotent, and guarded on `user_id IS NULL` rather than on `id = 1`: it is the
same single row today, but that predicate states the actual rule — an unowned
filter belongs to the single user — instead of the id=1 assumption this whole
sub-phase is removing. A second run therefore matches nothing, reports
"already attached", and can never re-point a row that already has an owner.

    python scripts/backfill_preferences_user.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Allow `python scripts/backfill_preferences_user.py` without installing the
# project as a package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.connection import get_crawler_conn  # noqa: E402
from db.users import resolve_current_user_id  # noqa: E402

# RETURNING id yields a row only on a genuine write, the same idiom
# scripts/seed_user.py and crawler/discover.TRACK_SQL use to count what they
# actually changed rather than what they attempted.
BACKFILL_SQL = """
UPDATE preferences
   SET user_id = $1
 WHERE user_id IS NULL
RETURNING id
"""


async def main() -> int:
    print("Connecting to Supabase via CRAWLER_DB_URL (session pooler)...")
    conn = await get_crawler_conn()
    try:
        # Raises with a message naming scripts/seed_user.py if MY_EMAIL is
        # unset or matches no users row. Resolving BEFORE the UPDATE means a
        # misconfigured environment changes nothing at all.
        user_id = await resolve_current_user_id(conn)

        attached = await conn.fetch(BACKFILL_SQL, user_id)

        total = await conn.fetchval("SELECT count(*) FROM preferences")
        unowned = await conn.fetchval(
            "SELECT count(*) FROM preferences WHERE user_id IS NULL"
        )
        mine = await conn.fetchval(
            "SELECT count(*) FROM preferences WHERE user_id = $1", user_id
        )
    finally:
        await conn.close()

    if attached:
        print(f"Attached {len(attached)} preferences row(s) to user {user_id}.")
    else:
        print("Nothing to attach — every preferences row already has an owner.")

    print(f"preferences: {total} row(s) total, {mine} owned by this user, {unowned} unowned.")

    # The gate on "1b is done". An unowned row means the migration is
    # half-applied and the filter it holds is reachable by nobody.
    if unowned:
        print(
            f"\nFAILED: {unowned} preferences row(s) still have user_id IS NULL.",
            file=sys.stderr,
        )
        return 1

    print("\nSUCCESS: no unowned preferences rows remain.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception as exc:  # noqa: BLE001 - top-level CLI error reporting
        print(f"\nFAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
