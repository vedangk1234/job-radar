"""Who "the current user" is — TEMPORARY, and deleted when Phase 2 lands.

THIS WHOLE MODULE IS A PHASE-2 BRIDGE
--------------------------------------
Phase 1 gives rows an owner; Phase 2 gives the app a way to know who is asking.
This module is the gap between the two, and it exists only because the second
half is not built yet: magic-link auth will hand every request a real session,
and at that point every caller of resolve_current_user_id() gets its user id
from the session instead and this file goes away.

Until then the answer is derived from MY_EMAIL, which is not a new identity
variable but the one the install already had — notify/digest.py sends the digest
to it, fetchers/http.py puts it in the User-Agent, and scripts/seed_user.py
created the users row from it. Deriving from that value rather than adding a
DEFAULT_USER_ID is deliberate: two variables that must agree about who you are
is two variables that can disagree, silently, with the failure showing up as an
empty digest rather than as an error.

NO CACHE HERE, ON PURPOSE — and note that dashboard/src/lib/currentUser.ts, the
TypeScript half of this same bridge, does memoise. The asymmetry is real rather
than an oversight: the Python side has entrypoints (run_digest, enrich_matches,
each script's main) that resolve once and pass the id down, so "one lookup per
run" is already structural. A module global would add hidden state that every
test has to remember to reset in order to buy a query that is already only run
once. The dashboard has no such entrypoint — every server component and route
handler is its own — so memoising is the only place that guarantee can live.
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

    Every failure here is loud, and that is the whole design. The rows this id
    selects are the filter that decides what reaches an inbox, so a lookup that
    quietly returned None would not fail — it would produce a digest for nobody,
    which reads exactly like a quiet week.
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
