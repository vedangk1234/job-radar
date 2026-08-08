"""A new account's starting filter is the same one in both languages.

Phase 2a is the first sub-phase in which a preferences row is created by
TypeScript. Until now the only one came from db/schema.sql's seed, in SQL, once,
on a database with a single user; from 2a lib/link-user.ts writes one inside the
transaction that creates each new account, seeded from
lib/labels.DEFAULT_PREFERENCES.

That makes those four values a THIRD copy of a thing notify/match.py already
states and db/schema.sql already seeds, and a third copy that nothing compares
is a copy that drifts. The drift would be quiet in the worst way: a new user's
digest would be computed from a filter subtly unlike the one this project
documents, nobody would see a stack trace, and the only symptom would be an
inbox that is slightly wrong forever.

`npm run match-parity` already keeps the MATCHER halves honest across the two
languages. This keeps the DEFAULTS honest, which match-parity cannot see —
it compares verdicts over the live table using whatever filter is stored, not
the filter a fresh account starts with.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from notify.match import (
    DEFAULT_CITIES,
    DEFAULT_EXCLUSIONS,
    DEFAULT_KEYWORDS,
    DEFAULT_MAX_LEVEL,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LABELS_TS = PROJECT_ROOT / "dashboard" / "src" / "lib" / "labels.ts"
LINK_USER_TS = PROJECT_ROOT / "dashboard" / "src" / "lib" / "link-user.ts"


def parsed_defaults() -> dict:
    """DEFAULT_PREFERENCES out of labels.ts, without running TypeScript.

    The object is plain data — string arrays and one integer — so it is read by
    isolating the literal and repairing the two ways TypeScript spells JSON
    differently: a trailing comma before a closing bracket, and the `as const`
    suffix. Anything more elaborate than that in this constant should fail here
    loudly rather than be parsed cleverly, because a computed default is a
    default nobody can read off the page.
    """
    text = LABELS_TS.read_text(encoding="utf-8")
    match = re.search(
        r"export const DEFAULT_PREFERENCES\s*=\s*(\{.*?\})\s*as const;", text, re.DOTALL
    )
    assert match, "DEFAULT_PREFERENCES not found in labels.ts as a plain object literal"

    literal = match.group(1)
    literal = re.sub(r"(\w+):", r'"\1":', literal)          # bare keys -> quoted
    literal = re.sub(r",(\s*[}\]])", r"\1", literal)         # trailing commas
    return json.loads(literal)


def test_cities_match():
    assert parsed_defaults()["cities"] == list(DEFAULT_CITIES)


def test_keywords_match():
    """Order included, not just membership.

    notify/match.py compiles these to word-boundary regexes and the first match
    wins nothing — order genuinely does not change the verdict. It is compared
    anyway because an order difference is the cheapest possible evidence that
    somebody edited one list and hand-copied the other, and the NEXT edit made
    that way is the one that drops a keyword.
    """
    assert parsed_defaults()["keywords"] == list(DEFAULT_KEYWORDS)


def test_exclusions_match():
    assert parsed_defaults()["exclusions"] == list(DEFAULT_EXCLUSIONS)


def test_max_level_matches():
    assert parsed_defaults()["max_level"] == DEFAULT_MAX_LEVEL


def test_the_defaults_are_not_empty():
    """An empty cities or keywords list matches no job at all.

    The preferences API rejects both for exactly this reason. This constant is
    the filter the INSTALL runs on, mirrored from the Python, and a version of it
    with an empty list would mean the digest silently matches nothing — which
    reads as "quiet week" rather than as a bug, the failure mode this whole
    project is arranged against.

    NOTE WHAT THIS NO LONGER SAYS, since Phase 3 Slice 1b. It used to be an
    argument about new accounts, because DEFAULT_PREFERENCES was also the SEED. A
    new account is now seeded from NEW_ACCOUNT_SEED, which is deliberately empty on
    every field — see the tests below for why that is the opposite of this failure
    rather than an instance of it. The short version: this constant describes the
    filter the INSTALL runs, where empty means a digest that silently matches
    nothing; that one describes a row nobody has configured yet, where empty is the
    honest state and onboarding_completed = FALSE is what stops it being used.
    """
    defaults = parsed_defaults()
    assert defaults["cities"], "an empty cities list matches no job at all"
    assert defaults["keywords"], "an empty keywords list matches no job at all"


# ---------------------------------------------------------------------------
# Phase 3 Slice 1b — the seed stops being the operator's filter.
#
# The bug these guard against had no symptom anybody could see. A new account was
# created with all 43 keywords above; PUT /api/preferences refuses more than
# KEYWORD_CAP = 25; and that same save is the only writer of
# onboarding_completed. So the seed refused the one act that finishes onboarding,
# and each of the three parts was individually correct.
#
# These are TEXTUAL, like the assertions in tests/test_dashboard_auth.py and
# tests/test_applied_column.py, and for the same reason: a rule about which
# constant a statement is allowed to name is checked against the statement's text
# or it is not checked at all. `npm run onboarding-check` is the other half —
# it carries a real account from signup to a completed save against the live
# database, which no amount of reading source text can do.
# ---------------------------------------------------------------------------


def labels_source() -> str:
    return LABELS_TS.read_text(encoding="utf-8")


def new_account_seed_body() -> str:
    """The literal's body, or a failure naming the constant rather than a regex."""
    body = re.search(
        r"export const NEW_ACCOUNT_SEED\s*=\s*\{(.*?)\}\s*as const;",
        labels_source(),
        re.DOTALL,
    )
    assert body, "NEW_ACCOUNT_SEED is not defined in labels.ts as a plain object literal"
    return body.group(1)


def test_the_new_account_seed_does_not_spread_the_defaults():
    """The spread came OUT, and putting it back is the regression to catch.

    Slice 1b wrote NEW_ACCOUNT_SEED as `{...DEFAULT_PREFERENCES, keywords: []}`,
    to stop three copied values drifting from the Python. That argument expired
    with the values: empty cannot drift, because there is no number or string here
    for notify/match.py to disagree with.

    What replaced it is the sharper hazard, and it runs the other way. A spread
    means every field DEFAULT_PREFERENCES gains in future arrives on new accounts
    as the operator's answer, silently and with nothing failing — which is exactly
    the bug this slice exists to end, re-entering through a constant nobody edited.
    Spelling the blanks out means a new field is absent from a new account's row
    until somebody deliberately adds it below.
    """
    text = new_account_seed_body()
    assert "...DEFAULT_PREFERENCES" not in text, (
        "NEW_ACCOUNT_SEED spreads DEFAULT_PREFERENCES again. A new account must "
        "start blank on every field, and a spread hands it whatever the install's "
        "filter happens to contain — including any field added to it later."
    )


def test_a_new_account_is_seeded_blank_on_every_list():
    """cities, keywords and exclusions are all the empty list.

    keywords is the assertion that would have caught the Slice 1b catch-22: any
    non-empty literal is somebody else's filter on a new person's row, and one over
    KEYWORD_CAP is the loop itself, back again. cities and exclusions are the same
    argument finished — a seeded city list is the operator's answer to "where do you
    want to work", and a seeded exclusion list silently drops the architect and
    analyst postings somebody might be looking for.

    All three columns are TEXT[] NOT NULL DEFAULT '{}', so the empty list is a legal
    value for each and none of them can fail the INSERT.
    """
    text = new_account_seed_body()
    for field in ("cities", "keywords", "exclusions"):
        assert re.search(rf"{field}\s*:\s*\[\s*\]", text), (
            f"NEW_ACCOUNT_SEED.{field} must be the empty list — onboarding is what "
            f"collects it, and anything seeded here is a preference the person "
            f"holding the account never chose"
        )


def test_the_new_account_seed_states_no_max_level():
    """The one field whose blank is an ABSENCE rather than an empty literal.

    preferences.max_level is `INT NOT NULL DEFAULT 2`, so NULL cannot mean "unset"
    for it — binding one would violate the constraint and fail the signup rather
    than blank the field. The schema default is the unset state, and branch 3
    reaches it by leaving the column out of its INSERT entirely.

    So naming max_level here is wrong in both possible spellings: a number is the
    operator's answer to "how senior", and a null is a signup that throws.
    """
    assert not re.search(r"^\s*max_level\s*:", new_account_seed_body(), re.MULTILINE), (
        "NEW_ACCOUNT_SEED names max_level. The column is NOT NULL DEFAULT 2, so the "
        "only legal 'unset' is the schema default — reached by omitting the column "
        "from the INSERT in lib/link-user.ts, not by binding a value from here."
    )


def test_link_user_inserts_only_the_three_blank_lists():
    """Branch 3's INSERT column list, checked against its own text.

    Two failures this catches, and they arrive from opposite directions. A column
    ADDED here is a field a new account is handed rather than asked about — the
    whole of this slice, reverted. max_level REAPPEARING is the specific one: it
    was removed so the NOT NULL DEFAULT 2 supplies it, and re-adding it means
    binding either the operator's 2 or a NULL that violates the constraint.

    The nine columns Phase 3 Step 0 added are covered by
    tests/test_phase3_columns.py's sweep, which forbids link-user.ts from naming
    any of them at all; their blanks come from their own schema defaults.
    """
    source = LINK_USER_TS.read_text(encoding="utf-8")
    insert = re.search(r"INSERT INTO preferences\s*\((.*?)\)", source, re.DOTALL)
    assert insert, "no INSERT INTO preferences found in link-user.ts"

    names = [c.strip() for c in insert.group(1).split(",")]
    assert names == ["user_id", "cities", "keywords", "exclusions"], (
        f"branch 3 inserts {names}. It must name only the owner and the three blank "
        f"lists — every other column on preferences has a schema default that is "
        f"already the blank a new account should start with."
    )


def test_the_cap_is_a_number_the_seed_can_satisfy():
    """Stated here so the two constants cannot drift apart silently.

    KEYWORD_CAP is in the same file, so a future edit lowering it to zero would
    reintroduce the loop with an empty seed. Nothing else in the app compares
    them.
    """
    cap = re.search(r"export const KEYWORD_CAP\s*=\s*(\d+)\s*;", labels_source())
    assert cap, "KEYWORD_CAP not found in labels.ts"
    assert int(cap.group(1)) > 0, (
        "KEYWORD_CAP must leave room for at least one keyword, or no account can "
        "ever complete onboarding"
    )


def test_link_user_seeds_from_the_new_account_seed_and_not_from_the_defaults():
    """The constant is only worth anything if it is the one branch 3 reads.

    Both names being in scope in that file is how a fix gets reverted by
    autocomplete: the INSERT is four interpolations, and one of them reverting to
    DEFAULT_PREFERENCES would put 43 keywords back on new rows while the constant
    added to prevent it sat unread beside them.
    """
    source = LINK_USER_TS.read_text(encoding="utf-8")
    assert "NEW_ACCOUNT_SEED" in source, "link-user.ts does not read NEW_ACCOUNT_SEED"

    offenders = [
        line.strip()
        for line in source.splitlines()
        if "DEFAULT_PREFERENCES" in line and not line.lstrip().startswith(("*", "//", "/*"))
    ]
    assert not offenders, (
        "link-user.ts still names DEFAULT_PREFERENCES in executable code:\n  "
        + "\n  ".join(offenders)
    )
