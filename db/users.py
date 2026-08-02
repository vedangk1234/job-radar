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
