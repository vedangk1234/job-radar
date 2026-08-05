"""Who "the current user" is — NARROWED IN 2b to the scripts that have no session.

WHAT THIS MODULE STOPPED BEING
------------------------------
Through Phase 1 this answered "whose filter, whose queue, whose dashboard" for
the entire install, because nothing could ask a session. Phase 2a gave the
dashboard a real one; Phase 2b took the two runtime paths off this module
altogether:

    the CRAWL   — crawler/enrich.py used to enrich against the MY_EMAIL user's
                  filter, which made the last stage of a user-blind crawl
                  user-aware. It now reads the union of every active account's
                  filter (notify/store.load_active_preferences) and resolves
                  nobody.
    the DIGEST  — notify/digest.run_digest used to default user_id to None and
                  resolve it here. The parameter is required now; the fan-out
                  reads its ids out of public.users and threads them down.

So no hourly cron step and no dashboard request reaches this file any more, and
tests/test_identity_bridge.py asserts that rather than trusting it.

WHAT IT STILL IS
----------------
The operator scripts' way of naming themselves. scripts/preview_digest.py,
scripts/recheck_descriptions.py, scripts/seed_user.py and the two backfills run
from a terminal with no request and no session, and each acts on the single
install the .env belongs to — so MY_EMAIL is the honest answer to "which account
is this laptop", and inventing a DEFAULT_USER_ID beside it would be two variables
that must agree about who you are and can disagree silently.

Note the one script that deliberately does NOT use this:
scripts/track_companies.py takes --email, because it acts on a NAMED person and
defaulting that to whoever the environment belongs to is how you quietly
configure your own account. Anything that acts on somebody else belongs on that
side of the line, not this one.

dashboard/scripts/current-user.ts is the TypeScript half of exactly this narrowed
use — headless checks, same variable, same lowercase comparison — and 2b moved it
out of src/ so that no request-path module can reach it at all.

NO CACHE HERE, ON PURPOSE. Each script has one main() that resolves once and
threads the id down, so "one lookup per run" is already structural; a module
global would add hidden state every test has to remember to reset in order to buy
a query that runs once anyway.
"""

from __future__ import annotations

from typing import Any

import os

from .connection import load_env

# .env.example ships this as the MY_EMAIL placeholder. Resolving it would look
# up an anchor row for nobody, which is the same class of failure as the digest
# refusing to send with the re_xxxx placeholder key.
PLACEHOLDER_EMAIL = "you@example.com"

# lower(email) on both sides, matching users_email_lower_idx in db/schema.sql:
# email identity is case-insensitive in practice, and a MY_EMAIL that differs
# from the seeded row only in case must resolve, not silently miss.
#
# Schema-qualified public.users, never a bare `users`. Supabase ships its own
# auth.users, so what a bare reference resolves to is a property of the
# connection's search_path rather than of this file, and db/connection.py sets
# no search_path at all. See the users block in db/schema.sql.
CURRENT_USER_SQL = "SELECT id FROM public.users WHERE lower(email) = lower($1)"

# Everyone the fan-out sends to. NOT part of the MY_EMAIL bridge — this one
# outlives Phase 2, because "which accounts get a digest this hour" is a question
# about the database rather than about whose laptop is running the script.
#
# is_active is the account-level off switch the users table has carried since 1a
# with nothing reading it. This is what reads it: a deactivated account keeps its
# preferences, its watch list and its notification history, and stops receiving
# email. Deleting the row would also stop the email and would take all three with
# it — which is a different decision, and one nobody can undo.
#
# ORDER BY lower(email) so a fan-out run is deterministic. Nothing depends on the
# order today; a run whose output moves between invocations for no reason is
# harder to compare against yesterday's, and the two-user verification in
# Phase 1e is exactly such a comparison.
DIGEST_USERS_SQL = """
SELECT id, email
  FROM public.users
 WHERE is_active = TRUE
 ORDER BY lower(email)
"""


def current_user_email() -> str:
    """MY_EMAIL from .env, lowercased, or a clear error naming what is missing.

    The single canonical validator. scripts/seed_user.py writes the users row
    from this and the bridge below reads it back, so a check that lived in only
    one of them would be a check the other could drift past — a seed that
    accepted the placeholder and a lookup that rejected it would disagree about
    whether the install is set up at all.
    """
    load_env()
    email = os.getenv("MY_EMAIL", "").strip().lower()

    if not email:
        raise RuntimeError(
            "MY_EMAIL is not set. It lives in .env (see .env.example) and is the "
            "same address notify/digest.py sends the digest to."
        )
    if email == PLACEHOLDER_EMAIL:
        raise RuntimeError(f"MY_EMAIL is still the .env.example placeholder ({PLACEHOLDER_EMAIL})")
    if "@" not in email:
        raise RuntimeError(f"MY_EMAIL does not look like an email address: {email!r}")

    return email


USER_EMAIL_SQL = "SELECT email FROM public.users WHERE id = $1"


async def email_for_user(conn, user_id: Any) -> str:
    """Where this user's digest goes. A property of the person, not of the run.

    Phase 1e needs this because the recipient stopped being a constant. Before
    the fan-out there was one digest and it went to MY_EMAIL; now there is one
    digest PER USER, and defaulting any of them to the environment would mean
    computing a second person's matches and posting them to my inbox — a failure
    that sends a real email, passes assert_self_send, and looks like a working
    system.

    Raises rather than returning None. The id was itself read out of this table,
    so a miss here is a row deleted mid-run or the wrong database entirely, and
    neither is something to send email through.
    """
    email = await conn.fetchval(USER_EMAIL_SQL, user_id)
    if email is None:
        raise RuntimeError(
            f"no public.users row for {user_id!r}: cannot decide where this "
            f"digest should go, so nothing is sent."
        )
    return str(email)


async def digest_users(conn) -> list[tuple[Any, str]]:
    """[(user_id, email)] for every active account, in a stable order.

    The fan-out's recipient list. An empty list is a real answer — an install
    with no active account sends nothing — and the caller reports it rather than
    treating it as a failure, because "nobody is due a digest" is the same kind of
    outcome as "nothing matched".

    Note what this does NOT do: check that each user has a preferences row or a
    watch list. Those are that user's own business and each has its own defined
    behaviour further down (a filter falls back, an empty watch list matches
    nothing). Answering "who exists" and "what do they want" in one query would
    make a user with no watch list indistinguishable from a user who was never
    there.
    """
    rows = await conn.fetch(DIGEST_USERS_SQL)
    return [(row["id"], row["email"]) for row in rows]


async def resolve_current_user_id(conn) -> Any:
    """The public.users id for MY_EMAIL. Raises rather than guessing.

    FOR scripts/ ONLY, since 2b. If you are writing anything the hourly cron runs
    — a crawl stage, a digest stage — the id you want is either not needed at all
    (enrichment is user-blind) or comes from db.users.digest_users, which reads
    the accounts that exist instead of the address this machine is configured
    with. Calling this from there would scope everybody's crawl to one person.

    Every failure here is loud, and that is the whole design. The rows this id
    selects are the filter a preview or a backfill acts on, so a lookup that
    quietly returned None would not fail — it would act on nobody, which reads
    exactly like a quiet week.
    """
    email = current_user_email()
    user_id = await conn.fetchval(CURRENT_USER_SQL, email)

    if user_id is None:
        raise RuntimeError(
            "MY_EMAIL does not match any row in public.users. Run "
            "`python scripts/seed_user.py` to create it — it reads the same "
            "environment variable this lookup does."
        )
    return user_id
