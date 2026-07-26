r"""Does this job deserve an email? Pure functions, no I/O, no side effects.

This module is the *only* place filtering happens. The crawler stores loosely —
every job, every city, unfiltered — precisely so that the decision to notify can
be made late, changed cheaply, and re-made against a table that never threw
anything away. Store loosely, notify tightly; this is the tight end.

The four tests are independent and combine with AND:

    city_ok         the stored city label is in preferences.cities
    keyword_ok      the title matches at least one keyword
    excluded        the title matches at least one exclusion       (negated)
    level_excluded  the title states a seniority level above max_level  (negated)

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


Why levels need their own test
------------------------------
The word exclusions catch 'senior', 'lead' and 'staff', and miss the other half
of how seniority is written down: "SDE III", "Software Engineer IV", "Backend
Engineer L4", "Engineer (Level 5)". Those titles say *senior* in a notation the
exclusion list cannot see, so they arrive in the inbox and waste the read.

detect_level reads that notation and level_excluded drops anything above
preferences.max_level (2 by default — level 1 and 2 are the jobs worth having).
A level is a number, which is what makes this the one filter in the file with a
real false-positive problem: job titles are *full* of numbers that mean the
opposite thing.

    "Ops Engineer 2 to 5 Years"          <- 2 and 5 are experience, not levels
    "Backend Engineer, 3+ years"         <- 3 is experience
    "Python Developer (2-4 years)"       <- both are experience

Dropping those would delete exactly the mid-level jobs this filter exists to
find, and silently. So every number in the title is first checked against the
experience shapes (years/yrs/y-o-e, "N to M", "N-M", slash lists) and masked out
if it matches; only what survives the mask is allowed to be read as a level.
The bias is deliberate and one-directional: an unrecognised level costs one line
in a digest, a misread experience range costs the job.
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

# Grounded in the resume rather than in what a job board happens to call things:
# every term here is a role title, a stack, or a discipline actually worth
# applying to. Grouped for reading, flat for matching.
DEFAULT_KEYWORDS = (
    # role titles
    "software engineer", "software developer", "software development engineer",
    "sde", "associate software engineer", "member of technical staff", "mts",
    "application developer", "programmer", "forward deployed engineer",
    # backend / services
    "backend", "back end", "node", "nodejs", "express", "fastapi",
    "api", "rest", "microservices", "distributed",
    # full stack / frontend
    "full stack", "fullstack", "frontend", "front end",
    "react", "react native", "nextjs", "next.js",
    # languages
    "python", "javascript", "typescript",
    # platform / infra
    "platform", "infrastructure", "infra", "devops", "cloud engineer",
    # ML / AI
    "machine learning", "ml engineer", "ai engineer", "mlops",
    "deep learning", "computer vision", "llm",
)

DEFAULT_EXCLUSIONS = (
    "intern", "senior", "staff", "principal", "lead", "manager",
    "director", "head", "architect", "sales", "recruiter", "support",
    "qa", "analyst",
)

# The highest stated seniority level that still reaches the inbox. 2 keeps
# "Software Engineer II" and "SDE 2" and drops "SDE III" and above.
DEFAULT_MAX_LEVEL = 2

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


# --- seniority level in the title -----------------------------------------

_ROMAN_LEVELS = {"ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6}

# Longest alternative first so "VI" is read as 6 and not as 5 followed by a
# stray 'i'; \b on both ends so 'v' fires only as a standalone token and never
# from inside "Java", "DevOps" or "v2". 'i' is deliberately absent — level 1 is
# kept anyway, and \bi\b would fire on the English word "I".
_ROMAN_RE = re.compile(r"\b(vi|iv|iii|ii|v)\b", re.IGNORECASE)

# The tokens a bare digit is allowed to be a level *for*. A digit that follows
# none of these and is not at the end of the title is not treated as a level.
_LEVEL_TOKEN = r"(?:sde|swe|sdet|mts|engineer|developer|analyst|grade)"

_DIGIT_LEVEL_RES = (
    re.compile(r"\bl([2-9])\b", re.IGNORECASE),                            # L3, L4
    re.compile(r"\blevel[\s\-]*([2-9])\b", re.IGNORECASE),                 # Level 5, level-4
    re.compile(rf"\b{_LEVEL_TOKEN}\b[\s\-]*([2-9])\b", re.IGNORECASE),     # SDE 3, Engineer-4
    re.compile(r"[\s\-]([2-9])\s*$"),                                      # "... 3" at the end
)

_NUMBER_RE = re.compile(r"\d+")

# A number is about EXPERIENCE rather than level when a duration unit follows it
# ("5 Years", "3+ yrs", "4 y-o-e") ...
_YEARS_AFTER_RE = re.compile(
    r"\s*\+?\s*(?:years?|yrs?|y\s*[-–—.]?\s*o\s*[-–—.]?\s*e)\b", re.IGNORECASE
)
# ... when a range or a slash list continues out of it ("2 to 5", "2-4", "2 / 3") ...
_RANGE_AFTER_RE = re.compile(r"\s*(?:to|[-–—/])\s*\d", re.IGNORECASE)
# ... or when one arrives at it. The hyphen form requires a digit on the far side,
# which is what keeps "SDE-4" a level while "2-4" is a range.
_RANGE_BEFORE_RE = re.compile(r"(?:\d\s*(?:to|[-–—/])\s*|\bto\s+)$", re.IGNORECASE)


def _mask_experience_numbers(text: str) -> str:
    """Blank out every number that denotes experience, leaving the rest in place.

    The replacement is the same length as what it replaces, so every offset — and
    therefore every \\b in the level patterns — still lines up with the original
    title. '#' is a non-word character, so a masked number cannot be read back as
    a level by any pattern above.

        "ops engineer 2 to 5 years"  ->  "ops engineer # to # years"
        "sde-4"                      ->  "sde-4"        (untouched)
    """
    out = list(text)
    for match in _NUMBER_RE.finditer(text):
        start, end = match.span()
        if (
            _YEARS_AFTER_RE.match(text, end)
            or _RANGE_AFTER_RE.match(text, end)
            or _RANGE_BEFORE_RE.search(text[:start])
        ):
            out[start:end] = "#" * (end - start)
    return "".join(out)


def detect_level(title: Any) -> int | None:
    """The seniority level the title states, or None if it states none.

    Only the four notations are recognised — roman numerals, L3, "level 3", and a
    digit attached to a role token or trailing the title. Anything else, and
    anything the experience mask removed, reads as "no level stated", which is
    kept rather than dropped.

    The maximum wins when a title states more than one, because "SDE II / III"
    is a III posting and the point of the filter is not to be surprised by that.
    """
    text = (title or "").lower()
    if not text:
        return None

    levels = [_ROMAN_LEVELS[m.group(1)] for m in _ROMAN_RE.finditer(text)]

    masked = _mask_experience_numbers(text)
    for pattern in _DIGIT_LEVEL_RES:
        levels.extend(int(m.group(1)) for m in pattern.finditer(masked))

    return max(levels) if levels else None


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
    max_level: int = DEFAULT_MAX_LEVEL

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

    @staticmethod
    def _as_level(row: Mapping[str, Any], fallback: int) -> int:
        """Read max_level out of a row that may predate the column.

        A database on the old schema has no max_level at all, and the honest
        answer there is the seeded default rather than a KeyError in the middle
        of a digest run — same reasoning as the absent-row fallback below.
        """
        try:
            value = row["max_level"]
        except (KeyError, IndexError, TypeError):
            return fallback
        try:
            return fallback if value is None else int(value)
        except (TypeError, ValueError):
            return fallback

    @classmethod
    def from_row(cls, row: Mapping[str, Any] | None) -> "Preferences":
        """Build from a preferences row; an absent row means the seeded defaults."""
        if row is None:
            return cls()
        return cls(
            cities=cls._as_tuple(row["cities"], DEFAULT_CITIES),
            keywords=cls._as_tuple(row["keywords"], DEFAULT_KEYWORDS),
            exclusions=cls._as_tuple(row["exclusions"], DEFAULT_EXCLUSIONS),
            max_level=cls._as_level(row, DEFAULT_MAX_LEVEL),
        )


# --- the four tests -------------------------------------------------------


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


def level_excluded(job: Mapping[str, Any], prefs: Preferences) -> bool:
    """The title states a level, and that level is above the one we asked for.

    "States a level" is the load-bearing half. A title with no level survives —
    most postings never number themselves, and treating silence as senior would
    empty the digest.
    """
    level = detect_level(job.get("title"))
    return level is not None and level > prefs.max_level


def is_match(job: Mapping[str, Any], prefs: Preferences) -> bool:
    """city AND keyword AND NOT excluded AND NOT level_excluded.

    Both exclusions are checked last and win outright: "Senior Backend Engineer,
    Bengaluru" and "SDE III, Bengaluru" each pass the first two tests and are
    still not jobs worth an email, because the point of the filter is a small
    inbox, not a complete one. They are two tests rather than one because they
    fail on different evidence — a word versus a number — and a preview run has
    to be able to say which.
    """
    return (
        city_ok(job, prefs)
        and keyword_ok(job, prefs)
        and not excluded(job, prefs)
        and not level_excluded(job, prefs)
    )


@dataclass
class MatchReport:
    """Why one job was kept or dropped — the preview's explanation, not the digest's."""

    job: Mapping[str, Any]
    matched: bool
    keywords_hit: list[str] = field(default_factory=list)
    exclusions_hit: list[str] = field(default_factory=list)
    level: int | None = None
    reason: str = ""


def explain(job: Mapping[str, Any], prefs: Preferences) -> MatchReport:
    """is_match, with its working shown. Same rules, same order, no second opinion."""
    keywords_hit = matched_terms(job.get("title"), prefs.keywords)
    exclusions_hit = matched_terms(job.get("title"), prefs.exclusions)
    level = detect_level(job.get("title"))

    if not city_ok(job, prefs):
        reason = f"city {job.get('city')!r} not in preferences"
    elif not keywords_hit:
        reason = "no keyword in title"
    elif exclusions_hit:
        reason = "excluded by " + ", ".join(repr(t) for t in exclusions_hit)
    elif level is not None and level > prefs.max_level:
        reason = f"level {level} above max_level {prefs.max_level}"
    else:
        reason = "matched " + ", ".join(repr(t) for t in keywords_hit)

    return MatchReport(
        job=job,
        matched=is_match(job, prefs),
        keywords_hit=keywords_hit,
        exclusions_hit=exclusions_hit,
        level=level,
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
