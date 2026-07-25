r"""Does this job deserve an email? Pure functions, no I/O, no side effects.

This module is the *only* place filtering happens. The crawler stores loosely —
every job, every city, unfiltered — precisely so that the decision to notify can
be made late, changed cheaply, and re-made against a table that never threw
anything away. Store loosely, notify tightly; this is the tight end.

The three tests are independent and combine with AND:

    city_ok      the stored city label is in preferences.cities
    keyword_ok   the title matches at least one keyword
    excluded     the title matches at least one exclusion  (negated)

Nothing here reads the database, the clock, or the environment. `preferences`
arrives as a value, `job` arrives as a mapping, and the answer is a bool — so
the whole notify decision is testable without a connection or a network.


Why word boundaries, not substrings
-----------------------------------
The naive `term in title.lower()` is wrong in both directions and the failures
are silent. 'go' is inside "Goa" and "Django"; 'api' is inside "Rapid" and
"Capital"; 'lead' is inside "Already" and "Leadership"; 'qa' is inside "Qatar".
A substring exclusion on 'qa' would drop every Bangalore backend role at a
company with "Qatar" in the location line, and nobody would ever notice — the
job simply never arrives.

So every term compiles to a regex anchored with \b on both ends. That makes
'go ' (whose trailing space in the seed list was itself a hand-rolled attempt at
a right boundary) match "Go Developer" and "Go/Python Engineer" but not "Goa"
and not "Golang" — 'golang' is a separate keyword, so nothing is lost.

Two deliberate consequences of \b, both accepted:

  * 'lead' matches "Lead Engineer" and "Engineering Lead" but NOT "Leadership"
    or "Leader". Exclusions are the dangerous direction — a wrong exclusion
    deletes a job from the inbox with no trace — so the narrow reading wins.
  * 'head' does not match "Headcount" or "Overhead". Same reasoning.

Multi-word terms ('back end', 'software engineer') join their tokens with
[\s\-_/]+ rather than a literal space, so one pattern covers "back end",
"back-end", "back_end" and "back/end". Recruiters spell it all four ways.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Iterable, Mapping, Sequence

# The values db/schema.sql seeds into the single preferences row. Kept here as
# well so tests and a --dry-run on a database that has not been migrated yet
# have something honest to fall back on. The DATABASE is the source of truth at
# runtime; this is a mirror, never an override.
DEFAULT_CITIES = ("blr", "pune", "hyd", "india-unclear")

DEFAULT_KEYWORDS = (
    "backend", "back end", "python", "golang", "go ", "platform",
    "infrastructure", "infra", "api", "distributed", "sde",
    "software engineer", "backend engineer", "full stack", "fullstack",
)

DEFAULT_EXCLUSIONS = (
    "intern", "senior", "staff", "principal", "lead", "manager",
    "director", "head", "architect", "sales", "recruiter", "support",
    "qa", "analyst",
)

# Inside a multi-word term, what may stand between the words.
_INNER_SEP = r"[\s\-_/]+"
_WS = re.compile(r"\s+")


@lru_cache(maxsize=512)
def compile_term(term: str) -> re.Pattern[str] | None:
    r"""One preference term -> a word-boundary regex, or None if it is empty.

    'go '            -> \bgo\b
    'back end'       -> \bback[\s\-_/]+end\b
    'c++'            -> \+\+ is escaped; \b would not fire against punctuation,
                        so terms whose edges are non-word characters fall back
                        to a plain escaped match rather than never matching.
    """
    tokens = _WS.split(term.strip().lower())
    tokens = [re.escape(t) for t in tokens if t]
    if not tokens:
        return None

    body = _INNER_SEP.join(tokens)
    # \b only means anything next to a word character. A term like 'c++' or '.net'
    # has a non-word edge, where \b would assert the opposite of what we want.
    left = r"\b" if re.match(r"\w", tokens[0]) else ""
    right = r"\b" if re.search(r"\w$", tokens[-1]) else ""
    return re.compile(f"{left}{body}{right}", re.IGNORECASE)


def matched_terms(title: Any, terms: Iterable[str]) -> list[str]:
    """Which of `terms` appear in `title`, as whole words. Order preserved.

    Returned rather than reduced to a bool because "why did this match?" is the
    question a preview run needs to answer, and re-deriving it later would mean
    a second, possibly divergent, implementation of the same rule.
    """
    text = (title or "").lower()
    if not text:
        return []
    return [t for t in terms if (p := compile_term(t)) is not None and p.search(text)]


@dataclass(frozen=True)
class Preferences:
    """The single preferences row, as a value object.

    Frozen because matching must not be able to edit the filter it is being
    judged against, and because that makes it safe to share across every job in
    a run.
    """

    cities: tuple[str, ...] = DEFAULT_CITIES
    keywords: tuple[str, ...] = DEFAULT_KEYWORDS
    exclusions: tuple[str, ...] = DEFAULT_EXCLUSIONS

    @staticmethod
    def _as_tuple(value: Any, fallback: Sequence[str]) -> tuple[str, ...]:
        """Coerce one TEXT[] column into a tuple of lowercased strings.

        asyncpg hands back a real list for TEXT[]. The SQLite test shim stores
        the same column as a JSON string, exactly as Company.from_row already
        copes with JSONB arriving as text. Both are accepted here so the
        production code path is the one the tests exercise.
        """
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                value = [value]
        if value is None:
            return tuple(fallback)
        return tuple(str(v).strip().lower() for v in value if str(v).strip())

    @classmethod
    def from_row(cls, row: Mapping[str, Any] | None) -> "Preferences":
        """Build from a preferences row; an absent row means the seeded defaults."""
        if row is None:
            return cls()
        return cls(
            cities=cls._as_tuple(row["cities"], DEFAULT_CITIES),
            keywords=cls._as_tuple(row["keywords"], DEFAULT_KEYWORDS),
            exclusions=cls._as_tuple(row["exclusions"], DEFAULT_EXCLUSIONS),
        )


# --- the three tests ------------------------------------------------------


def city_ok(job: Mapping[str, Any], prefs: Preferences) -> bool:
    """The job's stored city label is one we asked for.

    The label was computed at crawl time by fetchers.normalize.city_label and
    written to the row; it is read here, never recomputed. 'other' is the only
    label the default preferences drop, and 'india-unclear' is deliberately
    kept — a posting whose location string says "Remote" at an Indian company
    is a job worth seeing, and letting it through costs one line in a digest.
    """
    return (job.get("city") or "") in prefs.cities


def keyword_ok(job: Mapping[str, Any], prefs: Preferences) -> bool:
    """The title matches at least one keyword, as a whole word."""
    return bool(matched_terms(job.get("title"), prefs.keywords))


def excluded(job: Mapping[str, Any], prefs: Preferences) -> bool:
    """The title matches at least one exclusion, as a whole word."""
    return bool(matched_terms(job.get("title"), prefs.exclusions))


def is_match(job: Mapping[str, Any], prefs: Preferences) -> bool:
    """city AND keyword AND NOT excluded. The whole notify decision, in one line.

    Exclusion is checked last and wins outright: "Senior Backend Engineer,
    Bengaluru" passes both of the first two tests and is still not a job worth
    an email, because the point of the filter is a small inbox, not a complete one.
    """
    return city_ok(job, prefs) and keyword_ok(job, prefs) and not excluded(job, prefs)


@dataclass
class MatchReport:
    """Why one job was kept or dropped — the preview's explanation, not the digest's."""

    job: Mapping[str, Any]
    matched: bool
    keywords_hit: list[str] = field(default_factory=list)
    exclusions_hit: list[str] = field(default_factory=list)
    reason: str = ""


def explain(job: Mapping[str, Any], prefs: Preferences) -> MatchReport:
    """is_match, with its working shown. Same rules, same order, no second opinion."""
    keywords_hit = matched_terms(job.get("title"), prefs.keywords)
    exclusions_hit = matched_terms(job.get("title"), prefs.exclusions)

    if not city_ok(job, prefs):
        reason = f"city {job.get('city')!r} not in preferences"
    elif not keywords_hit:
        reason = "no keyword in title"
    elif exclusions_hit:
        reason = "excluded by " + ", ".join(repr(t) for t in exclusions_hit)
    else:
        reason = "matched " + ", ".join(repr(t) for t in keywords_hit)

    return MatchReport(
        job=job,
        matched=is_match(job, prefs),
        keywords_hit=keywords_hit,
        exclusions_hit=exclusions_hit,
        reason=reason,
    )


def select_matches(
    jobs: Iterable[Mapping[str, Any]], prefs: Preferences
) -> list[Mapping[str, Any]]:
    """The emailable subset, in the order given. Candidacy is the caller's problem.

    notified=FALSE and status='open' are enforced by the SELECT in notify/store.py,
    not here, because those are facts about the row's lifecycle rather than about
    whether the job is interesting.
    """
    return [job for job in jobs if is_match(job, prefs)]
