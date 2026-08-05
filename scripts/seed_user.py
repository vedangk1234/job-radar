"""Seed the single users row from MY_EMAIL.

The users table is the one thing db/schema.sql cannot seed itself. Every other
seed in that file is a literal — three city labels, a keyword list — but this
one is a real person's address, and "no hardcoded personal data, anywhere" is
enforced rather than requested: scripts/githooks/pre-commit blocks any address
outside the RFC 2606/6761 example domains, so the INSERT could not be committed
even if it were written. So the value comes from the environment, exactly as
notify/digest.py reads the digest recipient, and the repo never holds it.

That makes a fresh database reproducible: apply the schema, run this, and the
anchor row that Phase 1 hangs off is back.

Idempotent. ON CONFLICT (email) DO NOTHING, and RETURNING id therefore yields a
row only on a genuine insert — the same idiom crawler/discover.TRACK_SQL uses to
count what it actually created. Running this twice reports "already present" and
writes nothing, so it is safe on a live database with the row already in it.

    python scripts/seed_user.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Allow `python scripts/seed_user.py` without installing the project as a package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.connection import get_crawler_conn  # noqa: E402
from db.users import current_user_email  # noqa: E402

# email is lowered on the way in so the row agrees with users_email_lower_idx
# from the start. Because of that, conflicting on (email) is exactly equivalent
# to conflicting on lower(email) for anything this script writes. A row inserted
# by hand in some other case is the one thing that would raise here instead of
# being absorbed — which is the correct outcome, not a bug to paper over: two
# rows for one person is precisely what that index exists to prevent.
#
# Every reference below is schema-qualified as public.users, never a bare
# `users`. Supabase ships its own auth.users, so "users" is an ambiguous name on
# this database and what a bare reference resolves to is a property of the
# connection's search_path rather than of this file. db/connection.py sets no
# search_path at all, so today it resolves to public only because that is the
# server-side default for the role — a guarantee nothing here owns and nothing
# here would notice losing.
SEED_USER_SQL = """
INSERT INTO public.users (email, plan, is_active)
VALUES ($1, 'free', TRUE)
ON CONFLICT (email) DO NOTHING
RETURNING id
"""


async def main() -> int:
    # db.users.current_user_email is the one canonical validator, shared with the
    # operator-script lookup that reads this row back. It used to live here as
    # seed_email(); a second copy is how the writer and the reader end up
    # disagreeing about whether MY_EMAIL is usable.
    email = current_user_email()

    print("Connecting to Supabase via CRAWLER_DB_URL (session pooler)...")
    conn = await get_crawler_conn()
    try:
        user_id = await conn.fetchval(SEED_USER_SQL, email)
        total = await conn.fetchval("SELECT count(*) FROM public.users")
        existing_id = user_id or await conn.fetchval(
            "SELECT id FROM public.users WHERE email = $1", email
        )
    finally:
        await conn.close()

    if user_id is None:
        print(f"Already present: {email} (id {existing_id}) — nothing written.")
    else:
        print(f"Inserted: {email} (id {user_id}).")

    print(f"users now holds {total} row(s).")

    # Not an assertion. Phase 2 may legitimately add accounts; this only says so
    # out loud, because right now more than one row would mean a stray insert.
    if total != 1:
        print(f"NOTE: expected exactly 1 row while the install is single-user, found {total}.")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception as exc:  # noqa: BLE001 - top-level CLI error reporting
        print(f"\nFAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
