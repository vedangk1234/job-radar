"""Where "the current user" may still come from, after Phase 2b.

Phase 1 had one answer for the whole install: look MY_EMAIL up in public.users.
It was correct exactly as long as there was one account, and every sub-phase since
has been about replacing it with a real one — 1e gave the send path a list of
users to iterate, 2a gave the dashboard a session, and 2b takes the two remaining
runtime paths off the environment altogether.

What is left is deliberately narrow: the operator's own scripts, which run from a
terminal with no request and no session and act on the single install the .env
belongs to. This file draws that line where a reader can see it.

TEXTUAL, over the source, because the failure has no symptom. An enrichment pass
scoped to one person's filter enriches everything that person cares about, prints
a healthy-looking report, and is only wrong for somebody else — who is not the
person reading the log. Same for a digest defaulting to a user it was not given:
the email sends, the receipt is written, and the only evidence is in an inbox
nobody is watching. See tests/test_enrich.py for the behavioural half.
"""

from __future__ import annotations

from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# The bridge's two entry points, by name. Listed rather than matched on "user"
# because half this codebase legitimately says "user": these are the two
# functions that answer "who is running this process".
BRIDGE_NAMES = ("resolve_current_user_id", "current_user_email")

# Everything the hourly cron executes, from both entrypoints down. Listed by hand
# rather than globbed, so putting a module on the cron's path is a deliberate edit
# here — the same discipline tests/test_user_companies.CRAWL_PATH_FILES applies to
# the watch list.
#
# crawler/enrich.py is the module this phase is about. It resolved the MY_EMAIL
# user and enriched against that one person's filter, which made the last stage of
# a user-blind crawl user-aware; it now reads the union of every active account's
# filter and names no user at all.
#
# notify/digest.py is the other half. run_digest's user_id used to default to None
# and resolve here; it is required now, and run_fan_out has always read its ids out
# of public.users.
CRON_PATH_FILES = [
    "scripts/crawl.py",
    "scripts/send_digest.py",
    "crawler/loop.py",
    "crawler/store.py",
    "crawler/diff.py",
    "crawler/enrich.py",
    "notify/digest.py",
    "notify/store.py",
    "notify/match.py",
    "notify/description.py",
    "db/notifications.py",
    "db/watchlist.py",
    "fetchers/http.py",
    "fetchers/normalize.py",
    "fetchers/tier1.py",
    "fetchers/tier3.py",
    "fetchers/workday.py",
]

# The scripts that may still ask. Each has a main(), no session, and acts on the
# one install this .env describes — so MY_EMAIL is the honest answer to "which
# account is this laptop", not a stand-in for one nobody has built yet.
#
# scripts/track_companies.py is deliberately NOT here: it takes --email, because it
# acts on a NAMED person and defaulting that to whoever the environment belongs to
# is how you quietly configure your own account.
OPERATOR_SCRIPTS = [
    "scripts/preview_digest.py",
    "scripts/recheck_descriptions.py",
    "scripts/seed_user.py",
    "scripts/backfill_preferences_user.py",
    "scripts/backfill_user_companies.py",
]


def source(relative_path: str) -> str:
    return (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")


@pytest.mark.parametrize("relative_path", CRON_PATH_FILES)
@pytest.mark.parametrize("name", BRIDGE_NAMES)
def test_no_cron_path_module_resolves_identity_from_the_environment(relative_path, name):
    """Not "does not call it" — does not mention it at all.

    A name absent from the source cannot be imported, called, or reintroduced by
    an edit that looked local. The crawl has no user; the digest is handed one.
    """
    assert name not in source(relative_path), (
        f"{relative_path} names {name!r}: the cron's idea of who you are must come "
        f"from public.users (db.users.digest_users) or from nowhere at all, never "
        f"from MY_EMAIL"
    )


def test_the_crawl_path_asks_for_every_active_filter_rather_than_one():
    """The positive half: enrichment reads the union, and reads it once.

    Asserting only the absence above would be satisfied by an enrichment pass that
    silently stopped narrowing at all — which would fetch a description for every
    open row on the board, thousands of requests an hour, and pass a test about a
    name it no longer mentions.
    """
    enrich = source("crawler/enrich.py")

    assert "load_active_preferences" in enrich, (
        "crawler/enrich.py no longer reads the active filters — the pass has "
        "either lost its scope entirely or grown a new way to guess one"
    )
    assert "load_preferences" not in enrich.replace("load_active_preferences", ""), (
        "crawler/enrich.py loads a single user's preferences row again"
    )


def test_the_digest_will_not_guess_whose_digest_it_is():
    """run_digest's user_id has no default, stated against the signature.

    The behavioural version is in tests/test_digest.py; this is here so the rule
    reads alongside the others rather than only where the email is tested.
    """
    import inspect

    from notify.digest import run_digest

    parameter = inspect.signature(run_digest).parameters["user_id"]
    assert parameter.default is inspect.Parameter.empty, (
        "run_digest(user_id=...) has a default again: a default is a guess about "
        "whose matches these are, and guessing wrong sends a correct digest to the "
        "wrong person"
    )


@pytest.mark.parametrize("relative_path", OPERATOR_SCRIPTS)
def test_the_operator_scripts_still_have_a_way_to_name_the_operator(relative_path):
    """The exemption, enumerated rather than assumed — and asserted to still work.

    These are the reason db/users.py survives 2b. If one of them stops using the
    bridge, that is fine and good, but it should be a decision somebody made and
    not something discovered later from a stale docstring: the list here is what
    the module's docstring claims it is for.
    """
    text = source(relative_path)
    assert any(name in text for name in BRIDGE_NAMES), (
        f"{relative_path} no longer resolves the operator from MY_EMAIL — if that "
        f"is intended, take it off OPERATOR_SCRIPTS here and out of the list in "
        f"db/users.py's docstring, so the two do not disagree"
    )
