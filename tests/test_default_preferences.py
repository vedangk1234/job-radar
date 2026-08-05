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

    The preferences API rejects both for exactly this reason. A seed that
    shipped either would hand every new account a filter that silently matches
    nothing, and a digest that never arrives reads as "quiet week" rather than as
    a bug — which is the failure mode this whole project is arranged against.
    """
    defaults = parsed_defaults()
    assert defaults["cities"], "an empty cities list matches no job at all"
    assert defaults["keywords"], "an empty keywords list matches no job at all"
