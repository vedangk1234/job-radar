r"""Does this job deserve an email? Pure functions, no I/O, no side effects.

This module is the *only* place filtering happens. The crawler stores loosely —
every job, every city, unfiltered — precisely so that the decision to notify can
be made late, changed cheaply, and re-made against a table that never threw
anything away. Store loosely, notify tightly; this is the tight end.

The six tests are independent and combine with AND:

    city_ok              the stored city label is in preferences.cities
    keyword_ok           the title matches at least one keyword
    excluded             the title matches at least one exclusion            (negated)
    level_excluded       the title states a seniority level above max_level  (negated)
    pm_excluded          the description frames the role as management       (negated)
    experience_excluded  the description demands a minimum above 2 years     (negated)

Nothing here reads the database, the clock, or the environment. `preferences`
arrives as a value, `job` arrives as a mapping, and the answer is a bool — so
the whole notify decision is testable without a connection or a network.


Two stages, and why the seam is where it is
-------------------------------------------
The first four tests read the TITLE, which the crawler already stored. They are
free, and they run over every open job.

The last two read the DESCRIPTION, which costs a network request. So they read
columns rather than text: notify/description.py fetches each candidate's
description once, reduces it to `min_experience` and `is_pm_role`, stores those
two facts and discards the text. By the time this module sees a job, the answer
is already an integer and a boolean sitting on the row.

`title_stage_ok` names the seam explicitly, because the enrichment pass needs to
know which jobs are worth a request — the answer is "the ones that already pass
every title test", which is a handful per run rather than the whole board.

Both new tests inherit the same inclusion bias as everything else here:

    min_experience IS NULL  ->  KEEP

NULL covers three different situations — the description was never read, it was
read and stated no requirement, or the fetch failed — and all three mean the
same thing: we do not know, so do not delete the job. Only a number we are
confident about, above 2, drops a posting.


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
#
# `india-unclear` is deliberately NOT here. It used to be, on the argument that
# a "Remote" posting at an Indian company is worth one line of noise — and the
# argument was sound but the label was not: fetchers.normalize.city_label put
# every named Indian city into that bucket too, so "Lucknow", "Noida" and
# "Mohali" reached the inbox wearing the costume of an unknown location. That
# labelling is fixed (those are `other` now), and the gate is strict on top of
# it: three cities, and a posting that cannot prove it is in one of them does
# not match. Re-add 'india-unclear' from the preferences screen to get the old
# behaviour back — the row is the source of truth and this is only its mirror.
DEFAULT_CITIES = ("blr", "pune", "hyd")

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

# The most years of experience a description may demand and still be worth an
# email. A constant rather than a preferences column: max_level is about what a
# title CALLS the role, this is about what the description ASKS FOR, and the two
# have been wrong together often enough that conflating them would hide bugs in
# both. If it ever needs to be editable it becomes a column, exactly like
# max_level did.
MAX_MIN_EXPERIENCE = 2

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
    written to the row; it is read here, never recomputed.

    The gate is strict and has to be: a label not in the list fails, and so does
    a NULL one — `or ""` turns an unlabelled row into the empty string, which is
    in no preferences list, so "we never labelled it" can never read as a pass.
    This is the one test in the file with NO inclusion bias, because unlike a
    silent description a location string is always present and always readable:
    if the label is not one we asked for, that is an answer and not a gap.
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


def title_stage_ok(job: Mapping[str, Any], prefs: Preferences) -> bool:
    """The four title tests: city AND keyword AND NOT excluded AND NOT level.

    This is the set worth spending a network request on. notify/description.py
    fetches a description only for jobs that get TRUE from this function, which
    is what keeps the second stage at a handful of requests per run instead of
    one per row on the board.

    It is also, deliberately, the whole of the old is_match. Nothing about the
    title stage changed when the description stage was added; the new tests are
    two extra AND clauses, not a rewrite.
    """
    return (
        city_ok(job, prefs)
        and keyword_ok(job, prefs)
        and not excluded(job, prefs)
        and not level_excluded(job, prefs)
    )


def pm_excluded(job: Mapping[str, Any]) -> bool:
    """The stored is_pm_role flag. Computed by notify.description.detect_pm_role.

    Read, never recomputed — the same contract as `city`. The evidence was the
    description text, which no longer exists anywhere; the conclusion is the
    column. An unenriched row is FALSE, which means kept.
    """
    return bool(job.get("is_pm_role"))


def experience_excluded(job: Mapping[str, Any]) -> bool:
    """The description states a minimum, and that minimum is above the ceiling.

    "States a minimum" is the load-bearing half, exactly as with level_excluded.
    NULL is silence, and silence is kept: a fetch that failed, a description that
    never mentioned years, and a description nobody has read yet are all NULL,
    and dropping any of them would delete jobs for the reason that we did not
    look hard enough.

    A non-integer value is treated as silence too, rather than as an exception in
    the middle of a digest run.
    """
    years = min_experience_of(job)
    return years is not None and years > MAX_MIN_EXPERIENCE


def min_experience_of(job: Mapping[str, Any]) -> int | None:
    """The stored min_experience as an int, or None for absent/unreadable."""
    value = job.get("min_experience")
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def is_match(job: Mapping[str, Any], prefs: Preferences) -> bool:
    """The title stage AND NOT a PM role AND NOT a 3+-years role.

    Every exclusion is checked after the inclusions and each wins outright:
    "Senior Backend Engineer, Bengaluru", "SDE III, Bengaluru" and Razorpay's
    "Full Stack Builder" (a technical-PM role asking for 3-6 years) all pass the
    tests before the one that drops them. The point of the filter is a small
    inbox, not a complete one.

    They are four separate tests rather than one because they fail on four
    different kinds of evidence — a word in the title, a number in the title, a
    fact from the description, another fact from the description — and a preview
    run has to be able to say which.
    """
    return (
        title_stage_ok(job, prefs)
        and not pm_excluded(job)
        and not experience_excluded(job)
    )


@dataclass
class MatchReport:
    """Why one job was kept or dropped — the preview's explanation, not the digest's."""

    job: Mapping[str, Any]
    matched: bool
    keywords_hit: list[str] = field(default_factory=list)
    exclusions_hit: list[str] = field(default_factory=list)
    level: int | None = None
    min_experience: int | None = None
    is_pm_role: bool = False
    reason: str = ""


def explain(job: Mapping[str, Any], prefs: Preferences) -> MatchReport:
    """is_match, with its working shown. Same rules, same order, no second opinion."""
    keywords_hit = matched_terms(job.get("title"), prefs.keywords)
    exclusions_hit = matched_terms(job.get("title"), prefs.exclusions)
    level = detect_level(job.get("title"))
    min_experience = min_experience_of(job)
    is_pm = pm_excluded(job)

    if not city_ok(job, prefs):
        reason = f"city {job.get('city')!r} not in preferences"
    elif not keywords_hit:
        reason = "no keyword in title"
    elif exclusions_hit:
        reason = "excluded by " + ", ".join(repr(t) for t in exclusions_hit)
    elif level is not None and level > prefs.max_level:
        reason = f"level {level} above max_level {prefs.max_level}"
    elif is_pm:
        reason = "description frames this as a PM/management role"
    elif experience_excluded(job):
        reason = f"minimum {min_experience} years above ceiling {MAX_MIN_EXPERIENCE}"
    else:
        reason = "matched " + ", ".join(repr(t) for t in keywords_hit)

    return MatchReport(
        job=job,
        matched=is_match(job, prefs),
        keywords_hit=keywords_hit,
        exclusions_hit=exclusions_hit,
        level=level,
        min_experience=min_experience,
        is_pm_role=is_pm,
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
