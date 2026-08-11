r"""Does this job deserve an email? Pure functions, no I/O, no side effects.

This module is the *only* place filtering happens. The crawler stores loosely —
every job, every city, unfiltered — precisely so that the decision to notify can
be made late, changed cheaply, and re-made against a table that never threw
anything away. Store loosely, notify tightly; this is the tight end.

The six tests are independent and combine with AND:

    city_ok              the stored city label is one this account asked for
    keyword_ok           the title matches at least one keyword
    excluded             the title matches at least one exclusion            (negated)
    level_excluded       the title states a seniority level above max_level  (negated)
    pm_excluded          the description frames the role as management       (negated)
    experience_excluded  the stated minimum is outside the looking range     (negated)

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

The PM test inherits the same inclusion bias as everything else here: an
unenriched row is FALSE, which means kept.

The experience test USED TO, unconditionally, and no longer does — it asks the
account instead. Until Phase 3 Slice 2 the rule was a module constant,
MAX_MIN_EXPERIENCE = 2, and

    min_experience IS NULL  ->  KEEP

was a property of this file, true for everybody forever. NULL still covers the
same three situations — the description was never read, it was read and stated no
requirement, or the fetch failed — and they still all mean "we do not know". What
changed is who answers the question that follows. `show_no_experience_jobs` is
that answer, per account:

    min_experience IS NULL  ->  KEEP if prefs.show_no_experience_jobs else DROP

The inclusion bias is not abandoned, it is relocated: the install's own row says
TRUE (see scripts/backfill_onboarding_prefs.py, which seeded it precisely so that
wiring this column in could not silently start hiding every unenriched job), and
DEFAULT_SHOW_NO_EXPERIENCE_JOBS below keeps TRUE as the fallback for a row that
does not exist. It is worth knowing how much rests on it: on the live database
7,809 of 7,880 open rows have min_experience NULL, so this one boolean decides
99% of the board.

A confident number is now judged against a RANGE rather than a ceiling:

    exp_range_low <= min_experience <= exp_range_high

which drops a posting for being too JUNIOR as well as too senior. That is new —
under the constant no value could ever be too low. For the install's row the
range is (0, 2), which reproduces the old `<= 2` exactly, because min_experience
is never negative: {0,1,2} and "at most 2" select the same rows.

ONE CONSEQUENCE OF STORING ONLY A MINIMUM, accepted deliberately rather than
overlooked. There is no max_experience column — notify/description.py reduces
"3-6 years" to the integer 3 — so a job whose advertised band OVERLAPS the range
from below is still dropped: min 3 is outside (5, 8) although 5 and 6 are inside
the posting's own band. The alternative rule (`min_experience <= high`, today's
generalised) would keep it and would never fire the junior half at all. The
symmetric rule is the specified one; for a range of (0, 2) the two agree on every
stored row, and the cost is borne only by an account looking higher up.


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
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from typing import Any, Iterable, Mapping, Sequence

# The ONE import this module makes outside the standard library, and it is pure:
# fetchers.normalize is string rules and tables, no I/O of any kind, so "nothing
# here reads the database, the clock, or the environment" still holds. It is used
# by worth_a_description alone — the crawl's seam, never a verdict — which is why
# it does not need a TypeScript counterpart. See that function, and see the
# "the city label is not ported" note in CLAUDE.md.
from fetchers.normalize import india_funnel

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

# THE CITY GATE'S NEW INPUTS — Phase 3, cities Slice B.
#
# selected_cities and all_india arrived empty in Step 0 and are read here for the
# first time. `cities` above is NOT renamed and NOT deleted: it is the legacy
# column, it is still what the DEPLOYED digest reads, and it is still written on
# every save. See `Preferences.effective_cities` for how the two combine and for
# the sentence naming the slice that deletes the fallback.
#
# DEFAULT_ALL_INDIA IS FALSE, and that is the closed default rather than a
# neutral one. TRUE would mean a database with no preferences row at all emails
# every Indian posting on the board — the widest possible filter, arrived at by
# omission, which is the exact shape of failure the unconfigured experience range
# was given "matches nothing" to avoid.
DEFAULT_SELECTED_CITIES: tuple[str, ...] = ()
DEFAULT_ALL_INDIA = False

# The one label that is never India. Named once, here, because two rules read it
# and both mean the same thing by it: all_india is "anywhere except this", and a
# remote posting is allowed to ignore the city gate but is not allowed to ignore
# the country.
#
# STATED AS A COMPLEMENT RATHER THAN AS THE 25-ITEM MEMBERSHIP LIST IT IS
# EQUIVALENT TO, and the argument is fetchers.normalize.india_funnel's verbatim:
# a hand-written set of Indian labels works at 26 labels and becomes a trap at 27,
# because adding a city and forgetting to add it there silently NARROWS the rule
# for every all-India account, with nothing raising anywhere. There is exactly one
# label the question answers no to, so that is the one this file names.
NON_INDIA_LABEL = "non-india"

# THE LABELS AN EXPLICIT CITY SELECTION MAY NEVER MATCH — the fail-closed half of
# this slice, and the only place the city gate is not vocabulary-blind.
#
# `india-unclear` means "we could not tell": bare "Remote", bare "India", or a
# state that contains a city on the list. It is a statement about OUR confidence,
# not about the posting's location, and a person who ticked `chennai` did not ask
# to be told about postings that might be anywhere. So it matches all-India
# accounts — who have said the country is enough — and nobody else.
#
# `non-india` is here for the sharper reason: it is the one label all_india
# excludes by definition, and an explicit branch that could match it would mean
# the narrow filter admitting rows the wide one refuses. That is not a preference,
# it is an ordering violation.
#
# `other-india` is deliberately NOT here. It is a CONFIDENT statement that the
# posting is in India, at a named place outside the 23 — Lucknow, Mohali, Patna —
# so ticking it is a coherent thing to want ("anywhere else in India") and it
# carries none of the vagueness that disqualifies india-unclear.
#
# ENFORCED IN THREE PLACES, because the failure is silent in both directions: the
# editor does not offer these two, PUT /api/preferences refuses them in
# selected_cities, and this set is what makes it a PROPERTY rather than a promise
# for a row edited by hand in psql.
CITY_SELECTION_NEVER_MATCHES = frozenset({"india-unclear", NON_INDIA_LABEL})

# THE WORK ARRANGEMENT — jobs.work_mode on one side, preferences.work_modes on
# the other, and the two are read by two different rules that must not be
# confused with each other:
#
#   is_remote / city_ok    a REMOTE posting is exempt from the CITY gate. A
#                          statement about geography. Reads only jobs.work_mode.
#   work_mode_ok           this account will only accept these arrangements. A
#                          statement about preference. Reads both.
#
# Naming REMOTE_WORK_MODE separately from WORK_MODE_LABELS is deliberate: the
# relaxation cares about exactly one of the three values, and spelling that as
# `WORK_MODE_LABELS[2]` would make the rule depend on the order of a list.
REMOTE_WORK_MODE = "remote"

# The three values notify.description.detect_work_mode can produce, mirrored in
# dashboard/src/lib/labels.WORK_MODE_OPTIONS. No CHECK constrains the column (see
# db/schema.sql), so a fourth value arriving from a vendor is data to look at
# rather than a crash — and it matches nobody's selection, which is the safe way
# for an unrecognised value to fail.
WORK_MODE_LABELS = ("onsite", "hybrid", "remote")

# WHAT A NULL jobs.work_mode IS READ AS, AND WHY IT IS NOT WRITTEN DOWN.
#
# NULL means "nobody has established this posting's arrangement" — the
# description was never read, or it was read and stated nothing a rule could see.
# Most of the board is in that state and always will be, because a description is
# only ever fetched for a job some filter already wants (crawler/enrich.py).
#
# Read as onsite, so an unenriched job keeps behaving exactly as it did before
# this slice existed for anybody whose selection includes onsite — which is the
# default shape of the answer and what the operator's row says.
#
# NOT BACKFILLED INTO THE COLUMN, and that is the load-bearing half. Writing
# 'onsite' onto 9,600 rows nobody has looked at would record a conclusion as
# though it were an observation, and the table would lose the ability to tell
# "read it, it said onsite" from "never looked". That is jobs.notified exactly —
# one column carrying two facts — and the fix there cost four sub-phases. The
# distinction is preserved on the row and collapsed only here, at the moment of
# comparison, where collapsing it is a filtering decision rather than a record.
DEFAULT_WORK_MODE = "onsite"

# THE INSTALL'S OWN ANSWER, AND THAT IS NOT THE SAME THING AS "WHAT A BLANK ROW
# MEANS" — a distinction this file already draws twice and one it is easy to get
# backwards. This constant mirrors what scripts/backfill_onboarding_prefs.py
# wrote onto the operator's row (all three: nothing was filtered on mode), the
# same status DEFAULT_EXP_RANGE_LOW/_HIGH have. It is reached only by
# `Preferences()` with no row behind it — an un-migrated database, and the tests.
#
# "BLANK MATCHES NOTHING" LIVES IN from_row, NOT HERE, and the mechanism is
# _as_tuple's: a row that HAS the column and holds '{}' yields (), which is in no
# membership test, so that account matches nothing. Only a NULL or an absent
# column reaches this fallback, and both of those mean "a database older than the
# column" rather than "a person who ticked nothing".
#
# Getting this backwards is not a small error. Set to () it would mean a bare
# Preferences() — the fallback for a database with no preferences row at all —
# silently matched zero jobs, which is the "quiet week rather than an error"
# failure this codebase spends its guards on. It cost 85 red tests to notice,
# which is the guards working.
DEFAULT_WORK_MODES: tuple[str, ...] = ("onsite", "hybrid", "remote")

# THE PAY LADDER — preferences.salary_bands on one side, jobs.salary_min/max on
# the other, both in LPA (lakhs per annum).
#
# THE SIX IDENTIFIERS ARE NOT NEW AND MUST NOT BE RESPELT. They were written by
# scripts/backfill_onboarding_prefs.py when Phase 3 Step 0 landed, and that script
# has already run: the operator's row holds exactly these six strings today, and
# the script's own verification asserts the set matches. Minting prettier ones
# ("<10L", "50L+") would leave a live row full of labels the new vocabulary does
# not admit, and because this is a membership test the symptom would be silent —
# every PRICED job failing the gate, on a board where no job is priced yet, so it
# would ship green and bite on the first posting that stated pay. The display
# labels live in dashboard/src/lib/labels.SALARY_BAND_OPTIONS, which is the same
# split `blr` (stored) and "Bengaluru" (shown) already have.
#
# HALF-OPEN AND CONTIGUOUS, [low, high), with 50+ unbounded. Written as a mapping
# to bounds rather than as parallel lists so a band cannot acquire a range that
# disagrees with its name, and so the gap-free property is checkable by reading
# one column. None means "no ceiling" rather than a large number, because a large
# number is a value somebody would eventually have to argue about.
SALARY_BAND_LABELS: tuple[str, ...] = ("<10", "10-15", "15-25", "25-40", "40-50", "50+")

SALARY_BAND_BOUNDS: dict[str, tuple[Decimal, Decimal | None]] = {
    "<10": (Decimal(0), Decimal(10)),
    "10-15": (Decimal(10), Decimal(15)),
    "15-25": (Decimal(15), Decimal(25)),
    "25-40": (Decimal(25), Decimal(40)),
    "40-50": (Decimal(40), Decimal(50)),
    "50+": (Decimal(50), None),
}

# THE INSTALL'S OWN ANSWER — all six, mirroring what backfill_onboarding_prefs.py
# wrote and what the live row holds. Reached only by a bare `Preferences()` with
# no row behind it, exactly like DEFAULT_WORK_MODES.
#
# AND UNLIKE DEFAULT_WORK_MODES, THE BLANK CASE AGREES WITH IT. work_modes has a
# sharp difference between "the fallback for a database older than the column"
# (all three) and "a person who ticked nothing" (matches nothing); for salary both
# resolve to "do not filter on pay". That is not an oversight in the parallel —
# it is the fail-open decision, and salary_ok is where it is written down.
DEFAULT_SALARY_BANDS: tuple[str, ...] = SALARY_BAND_LABELS

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

# THE EXPERIENCE RULE, WHICH USED TO BE A CONSTANT AND IS NOW THREE COLUMNS.
#
# What stood here was `MAX_MIN_EXPERIENCE = 2`, with a comment ending "If it ever
# needs to be editable it becomes a column, exactly like max_level did." This is
# that. Phase 3 Step 0 created preferences.exp_range_low / exp_range_high /
# show_no_experience_jobs and deliberately left them unread; Slice 2 is the
# feature step that reads them.
#
# THE CONSTANT WAS DELETED RATHER THAN LEFT STANDING, which is the jobs.notified
# lesson applied to a name in Python instead of a column in Postgres: a value
# written by nobody and read by nobody is worse than absent, because the next
# person to find it reasonably assumes something depends on it and the first edit
# made on that assumption is a silent one.
#
# max_level is STILL a separate axis and is untouched by this. It is about what a
# title CALLS the role; these are about what the description ASKS FOR, and the two
# have been wrong together often enough that conflating them would hide bugs in
# both. That both defaults happen to be 2 is a coincidence.
#
# THESE THREE ARE A MIRROR, NEVER AN OVERRIDE — the same status DEFAULT_CITIES has
# above. The database is the runtime source of truth. What they mirror is not a
# db/schema.sql seed (the columns arrive NULL / NULL / FALSE, which is
# "unconfigured") but the values scripts/backfill_onboarding_prefs.py wrote onto
# the install's existing row, chosen there to reproduce the old fixed rule to the
# character. They are reached only by Preferences() with no row behind it: an
# un-migrated database, and the tests.
#
# WHY THE FALLBACK FOR THE TOGGLE IS TRUE WHILE THE COLUMN DEFAULT IS FALSE, and
# the asymmetry is deliberate on both ends. FALSE is right for a column on a row
# nobody has answered yet — it is not a decision, and onboarding_completed is what
# says so. TRUE is right for "there is no row at all", because that is the
# un-migrated database whose digest has been running on "unknown means keep" all
# along, and flipping it by omission would hide 99% of the board.
DEFAULT_EXP_RANGE_LOW = 0
DEFAULT_EXP_RANGE_HIGH = 2
DEFAULT_SHOW_NO_EXPERIENCE_JOBS = True

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
    a run. Frozen also makes it hashable, which notify/store.load_active_
    preferences relies on to deduplicate identical filters — the three fields
    added in Slice 2 are an int, an int and a bool, so that still holds.

    NINE FIELDS SINCE CITIES SLICE B, AND THE ONE THAT IS DELIBERATELY ABSENT.
    preferences.years_of_experience_held is NOT here, and it is not an oversight.
    It is informational — how much experience the person HAS, which the profile
    screen collects and shows — and it must never filter anything. Keeping it off
    this object is what makes that a PROPERTY rather than a promise: is_match
    reads nothing else, so a column that never reaches here cannot reach a
    verdict however some future loader is written. notify/store.PREFERENCES_SQL
    correspondingly does not select it, and tests/test_phase3_columns.py asserts
    that no matcher in either language so much as names it.

    exp_range_low and exp_range_high are OPTIONAL ints because NULL is a real and
    meaningful value on those columns: "this account has not configured a range".
    See experience_excluded for what that resolves to, and why it is not the
    seeded default.

    THREE CITY FIELDS RATHER THAN ONE, FOR EXACTLY ONE SLICE. `cities` is the
    legacy column, `selected_cities` is what a person ticked, `all_india` is the
    switch that ignores both. effective_cities resolves the first two; the
    contract slice deletes `cities` and this paragraph with it.
    """

    cities: tuple[str, ...] = DEFAULT_CITIES
    selected_cities: tuple[str, ...] = DEFAULT_SELECTED_CITIES
    all_india: bool = DEFAULT_ALL_INDIA
    keywords: tuple[str, ...] = DEFAULT_KEYWORDS
    exclusions: tuple[str, ...] = DEFAULT_EXCLUSIONS
    max_level: int = DEFAULT_MAX_LEVEL
    exp_range_low: int | None = DEFAULT_EXP_RANGE_LOW
    exp_range_high: int | None = DEFAULT_EXP_RANGE_HIGH
    show_no_experience_jobs: bool = DEFAULT_SHOW_NO_EXPERIENCE_JOBS
    work_modes: tuple[str, ...] = DEFAULT_WORK_MODES
    salary_bands: tuple[str, ...] = DEFAULT_SALARY_BANDS

    @property
    def effective_cities(self) -> tuple[str, ...]:
        """The city list the gate actually reads: the new column, else the old one.

        THE MIGRATION-WINDOW FALLBACK, AND IT HAS AN EXPIRY DATE. Slice B moves the
        gate from `cities` to `selected_cities`, and the two columns are populated
        by different things: `selected_cities` by
        scripts/backfill_onboarding_prefs.py and by every save through
        PUT /api/preferences since this slice, `cities` by every save ever made.
        A row that predates the backfill has the second and not the first, and
        reading only the new column would silently empty that account's digest —
        the "quiet week rather than an error" failure this project spends its
        guards on.

        SO WHY IS THIS NOT THE "FILTER NOBODY CHOSE" MISTAKE Slices 1b and 1c
        removed? Because it is not a fallback to somebody ELSE'S answer. It is a
        fallback to THIS account's own previous answer, sitting in the column that
        answer was written to. The failure those slices fixed was handing a
        stranger the operator's three cities; this hands a person their own.

        IT CANNOT RE-OPEN THE BLANK-ACCOUNT HOLE, which is the thing to check
        before believing the paragraph above. A brand-new account has BOTH columns
        empty (NEW_ACCOUNT_SEED writes `cities: []`, and selected_cities takes its
        schema default of '{}'), so the fallback resolves to () and the gate
        matches nothing — fail-closed, exactly as specified, with no special case.

        THE CONTRACT SLICE DELETES THIS. Once PUT has written selected_cities for
        every account that has ever saved, `cities` is vestigial: the next slice
        stops writing it, drops the column, and this property collapses to a field
        read. It is written as one expression in one place precisely so that
        deletion is a deletion rather than an audit.
        """
        return self.selected_cities or self.cities

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

    @staticmethod
    def _as_optional_int(row: Mapping[str, Any], key: str) -> int | None:
        """Read a nullable INT column, PRESERVING the NULL. No fallback anywhere.

        THE ONE READER IN THIS CLASS THAT DOES NOT FALL BACK, and the difference
        from _as_level above is the whole point rather than an inconsistency.

        max_level is `INT NOT NULL DEFAULT 2`, so a NULL there cannot mean
        anything a person chose — it can only mean a database old enough not to
        have the column, and the seeded default is the honest answer. exp_range_low
        and exp_range_high are nullable BY DESIGN (db/schema.sql says why: "NULL
        means not configured, which is true of every existing row and of every
        account created before the onboarding screen exists"), so NULL is a value
        with a meaning and replacing it with 0..2 would be handing this account the
        operator's answer — exactly the failure Slices 1b and 1c spent two commits
        removing from keywords, cities, exclusions and max_level.

        A missing key resolves to None for the same reason rather than raising:
        notify/store.PREFERENCES_SQL names both columns, so a KeyError here would
        mean a database without them, where the SELECT would already have failed —
        and "unconfigured" is the safe reading of a column that is not there.
        """
        try:
            value = row[key]
        except (KeyError, IndexError, TypeError):
            return None
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _as_tuple_column(
        cls, row: Mapping[str, Any], key: str, fallback: Sequence[str]
    ) -> tuple[str, ...]:
        """_as_tuple, but reading a column that may not be in the row at all.

        Needed for selected_cities and nothing else. The four original list columns
        are indexed directly because a row without them is not a database this code
        can run against; selected_cities is different only in that a caller may
        legitimately hand over a row from before Slice B — the SQLite mirror in a
        test, a --dry-run against an un-migrated database — and the honest answer
        there is the fallback, which is empty, which makes effective_cities defer to
        the legacy column. A KeyError in the middle of a digest run is not.
        """
        try:
            value = row[key]
        except (KeyError, IndexError, TypeError):
            return tuple(fallback)
        return cls._as_tuple(value, fallback)

    @staticmethod
    def _as_bool(row: Mapping[str, Any], key: str, fallback: bool) -> bool:
        """Read show_no_experience_jobs, which is NOT NULL and so may fall back.

        Same shape as _as_level and for the same reason: the column is
        `BOOLEAN NOT NULL DEFAULT FALSE`, so a NULL or an absent key means a row
        that predates it, where the fallback is the only honest answer. The SQLite
        test shim stores it as an INTEGER, which bool() reads correctly.
        """
        try:
            value = row[key]
        except (KeyError, IndexError, TypeError):
            return fallback
        return fallback if value is None else bool(value)

    @classmethod
    def from_row(cls, row: Mapping[str, Any] | None) -> "Preferences":
        """Build from a preferences row; an absent row means the seeded defaults.

        Note the asymmetry between the two halves, which is stated on each reader
        above: the four columns that have always decided fall back to their
        mirrors when NULL, and the two nullable experience ints do not. A real row
        saying NULL is answering "nobody has configured this", and that answer has
        to survive being read.
        """
        if row is None:
            return cls()
        return cls(
            cities=cls._as_tuple(row["cities"], DEFAULT_CITIES),
            selected_cities=cls._as_tuple_column(
                row, "selected_cities", DEFAULT_SELECTED_CITIES
            ),
            all_india=cls._as_bool(row, "all_india", DEFAULT_ALL_INDIA),
            keywords=cls._as_tuple(row["keywords"], DEFAULT_KEYWORDS),
            exclusions=cls._as_tuple(row["exclusions"], DEFAULT_EXCLUSIONS),
            max_level=cls._as_level(row, DEFAULT_MAX_LEVEL),
            exp_range_low=cls._as_optional_int(row, "exp_range_low"),
            exp_range_high=cls._as_optional_int(row, "exp_range_high"),
            show_no_experience_jobs=cls._as_bool(
                row, "show_no_experience_jobs", DEFAULT_SHOW_NO_EXPERIENCE_JOBS
            ),
            work_modes=cls._as_tuple_column(row, "work_modes", DEFAULT_WORK_MODES),
            # The fallback is reached only by a row from before the column
            # existed, and it is the same value an empty selection resolves to
            # behaviourally — see DEFAULT_SALARY_BANDS. This is the one column
            # here where "older than the migration" and "ticked nothing" are
            # allowed to mean the same thing, because for pay they genuinely do.
            salary_bands=cls._as_tuple_column(row, "salary_bands", DEFAULT_SALARY_BANDS),
        )


# --- the four tests -------------------------------------------------------


def is_remote(job: Mapping[str, Any]) -> bool:
    """The posting is fully remote, per the stored jobs.work_mode label.

    READ, NEVER RECOMPUTED — the same contract `city`, `is_pm_role` and
    `min_experience` follow. The evidence is a description or a location string;
    the conclusion is the column.

    THIS IS LIVE AS OF THE WORK-MODE SLICE. It was built by cities Slice B and
    could not fire, because jobs.work_mode was NULL on all 9,703 stored rows;
    notify/description.py now writes the column for every job whose description
    is read, so the branch in city_ok below turns on by data rather than by an
    edit to the rule — which is exactly what it was built early for.

    NOTE WHAT THIS DOES *NOT* USE: DEFAULT_WORK_MODE. work_mode_of below reads a
    NULL as onsite for the purpose of comparing against a person's selection;
    this reads it as "not remote" by simply failing the equality. The two agree
    on today's board — onsite is not remote either way — and they are written
    separately because they answer different questions and could one day disagree.

    NULL READS AS NOT-REMOTE RATHER THAN AS UNKNOWN-MEANS-KEEP, which is the
    opposite of what min_experience does with its NULL, and the asymmetry is
    deliberate. The remote branch RELAXES a gate: reading "we have not parsed this
    yet" as "fully remote" would drop the city filter for the entire board, which
    is the widest possible failure reached by the emptiest possible evidence.
    city_ok has no inclusion bias, and this is that rule applied to a second
    column.
    """
    return (job.get("work_mode") or "") == REMOTE_WORK_MODE


def work_mode_of(job: Mapping[str, Any]) -> str:
    """The stored arrangement, with a NULL read as onsite. Never written back.

    See DEFAULT_WORK_MODE for why the collapse happens here and not in the table.
    The value is lowercased and stripped because it is compared against a
    person's selection, which lib/preferences.cleanList has already normalised —
    two sides of a membership test have to agree about case or the test is a
    coin flip.
    """
    return (job.get("work_mode") or "").strip().lower() or DEFAULT_WORK_MODE


def work_mode_ok(job: Mapping[str, Any], prefs: Preferences) -> bool:
    """This posting's arrangement is one this account said it would accept.

    A plain membership test over opaque strings, exactly as the explicit branch of
    city_ok is, and vocabulary-blind for the same reason: the labels live in
    notify/description.py and in the picker, and this rule does not need to know
    what they mean.

    BLANK MATCHES NOTHING — the fourth member of a family. An unconfigured
    experience range, a blank city answer, and now an empty work_modes all
    resolve the same way, and the argument has not changed since Slice 1b: a
    filter nobody chose, applied silently, is the expensive failure, and handing
    this account somebody else's three ticks would be exactly that. It is
    unreachable for an onboarded account by construction — PUT /api/preferences
    refuses an empty selection and is the only writer of this column AND of
    onboarding_completed, so onboarded implies configured.

    It EXCLUDES rather than raising, because notify.digest.run_fan_out catches
    only DigestError and an exception here would take every account's digest down
    over one account's blank row.
    """
    return work_mode_of(job) in prefs.work_modes


def salary_of(job: Mapping[str, Any]) -> tuple[Decimal | None, Decimal | None]:
    """The stored pay range in LPA, or (None, None) for a posting with no price.

    Read, never recomputed — the same contract `city`, `is_pm_role`,
    `min_experience` and `work_mode` follow. The evidence was a description, which
    no longer exists anywhere; the conclusion is the column.

    A HALF-STATED RANGE IS COMPLETED FROM THE OTHER END rather than left ragged.
    "₹18,00,000 and up" parses to (18, None) and means everything from 18 upward,
    so the missing bound is an absence of a ceiling and not an absence of
    information — but salary_ok wants two numbers, so the open end is represented
    by None and handled there. Both being None is the only state that means "no
    price", which is what makes the test in salary_ok a single `is None` pair
    rather than a case analysis.

    Values arrive as Decimal from asyncpg's NUMERIC and as float from the SQLite
    mirror's REAL; both are coerced through str() so the comparison arithmetic
    happens in one type. A junk value reads as no price, rather than raising in
    the middle of a digest run — the accommodation min_experience_of makes.

    TRAILING ZEROS ARE STRIPPED, AND THAT IS A PARITY REQUIREMENT RATHER THAN
    TIDINESS. NUMERIC keeps whatever scale it was given, so a column holding 18.00
    hands back Decimal('18.00') whose str() is "18.00" — while the TypeScript port
    reads the same column through postgres.js, which returns NUMERIC as a STRING,
    parses it to the number 18, and renders "18". explain() interpolates this
    value into a reason both languages must produce character for character, so
    the two representations have to be reconciled somewhere, and here is the only
    place both sides pass through.
    """
    def read(key: str) -> Decimal | None:
        value = job.get(key)
        if value is None:
            return None
        try:
            amount = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            return None
        integral = amount.to_integral_value()
        return integral if amount == integral else amount.normalize()

    return read("salary_min"), read("salary_max")


def salary_ok(job: Mapping[str, Any], prefs: Preferences) -> bool:
    """This posting's pay overlaps a band this account ticked. Three branches.

        no price on the job     ->  SHOW, whatever the bands say
        no bands ticked         ->  SHOW, no pay filtering at all
        otherwise               ->  the job's range OVERLAPS a selected band

    THE FIRST BRANCH IS THE WHOLE RULE IN PRACTICE. Every one of the ~9,800 stored
    rows is unpriced and will stay that way: 36 of 36 live candidate descriptions
    were read cleanly and not one stated a number (notify/description.py carries
    the measurement). So this gate is a no-op on today's board BY DATA, and the
    synthetic probes in dashboard/scripts/match-parity.ts are the only thing that
    can prove it fires at all. That is not a reason to weaken it — it is the reason
    the second branch below points the way it does.

    A BLANK SELECTION SHOWS EVERYTHING, AND THIS IS THE ONE PLACE THIS FILE
    DELIBERATELY BREAKS ITS OWN FAIL-CLOSED PATTERN. The unconfigured experience
    range, the blank city answer and an empty work_modes all match NOTHING, and
    the argument there has been the same since Slice 1b: a filter nobody chose,
    applied silently, is the expensive failure. Four reasons pay is genuinely a
    different kind of question rather than a fourth instance of that one:

    1. THE FAILURE THOSE THREE GUARD AGAINST IS UNREACHABLE HERE. They fail closed
       because a blank filter would otherwise widen an inbox on an answer nobody
       gave. Failing OPEN on pay widens nothing that the first branch has not
       already shown — an unpriced job is visible either way — so the blast radius
       of this choice on the current board is exactly zero rows.

    2. FAIL-CLOSED WOULD CONTRADICT THE FIRST BRANCH. The rule already shows a job
       whose EMPLOYER said nothing about pay. Hiding a job because the READER said
       nothing would mean silence on one side of the same question admitting a job
       and silence on the other side deleting it — the ordering violation
       CITY_SELECTION_NEVER_MATCHES exists to prevent, mirrored.

    3. THE JOB SIDE IS SILENT ALMOST ALWAYS, so "no opinion about pay" is this
       filter's resting state rather than an unanswered question. That is not true
       of a city or an arrangement, where the job side always has an answer and a
       blank filter really is somebody who has not been asked yet.

    4. IT IS THEREFORE OPTIONAL AT THE ROUTE TOO, and that consequence is stated
       rather than left to be discovered: PUT /api/preferences does NOT refuse an
       empty salary_bands, so "onboarded implies configured" — true of the city,
       range and mode columns by construction — DOES NOT EXTEND TO THIS ONE. It
       joins `exclusions` and `years_of_experience_held` as a field a person may
       legitimately leave blank forever.

    OVERLAP, NOT CONTAINMENT, and the difference decides real jobs. A posting
    advertised at 18-24 LPA overlaps `15-25` and it also overlaps `10-15`, because
    part of what it offers falls in each. Requiring containment would hide a job
    from somebody whose band it partly meets, which is the same "a range that
    partly fits is a fit" reasoning that governs the experience rule's floor —
    except pay is a band a person is willing to accept rather than a threshold
    they must clear, so the generous reading is the correct one rather than merely
    the safe one.
    """
    low, high = salary_of(job)
    if low is None and high is None:
        return True
    if not prefs.salary_bands:
        return True

    # A half-open job range: an absent end is unbounded on that side. `18 LPA and
    # up` must reach 50+, and a posting stating only a ceiling must reach <10.
    job_low = low if low is not None else Decimal(0)
    job_high = high if high is not None else None

    for band in prefs.salary_bands:
        bounds = SALARY_BAND_BOUNDS.get(band)
        if bounds is None:
            # An unrecognised band matches nothing rather than raising — the same
            # way an unrecognised work mode simply fails its membership test. A
            # value this file does not know is data to look at, not a crash in the
            # middle of a digest.
            continue
        band_low, band_high = bounds
        # Overlap of [job_low, job_high] with [band_low, band_high), both ends
        # possibly unbounded. Written as the two conditions that must BOTH hold
        # rather than as the negation of "disjoint", because the ends are nullable
        # and a De Morgan over nullable bounds is where this kind of test goes
        # wrong silently.
        starts_before_band_ends = band_high is None or job_low < band_high
        ends_after_band_starts = job_high is None or job_high >= band_low
        if starts_before_band_ends and ends_after_band_starts:
            return True
    return False


def city_ok(job: Mapping[str, Any], prefs: Preferences) -> bool:
    """The job's location is one this account asked for. Three branches.

    The label was computed at crawl time by fetchers.normalize.city_label and
    written to the row; it is read here, never recomputed.

        remote or all-India  ->  anywhere except non-india
        an explicit list     ->  membership, minus the two labels that never match

    The gate is strict and has to be: a label not in the list fails, and so does
    a NULL one — `or ""` turns an unlabelled row into the empty string, which is
    in no preferences list, so "we never labelled it" can never read as a pass.
    This is the one test in the file with NO inclusion bias, because unlike a
    silent description a location string is always present and always readable:
    if the label is not one we asked for, that is an answer and not a gap.

    THE TWO RELAXING BRANCHES SHARE A LINE, AND THAT IS THE POINT. all_india and
    a remote posting are different questions — "I do not care which city" and
    "this job has no city" — and they have the same answer: any label except
    non-india. Writing it once means the country floor cannot be enforced in one
    and forgotten in the other, which is the shape of every leak in this file's
    history.

    WHY non-india IS EXCLUDED FROM THE RELAXING BRANCHES BUT NOT TESTED AT THE
    TOP. A person who explicitly ticks a city gets a plain membership test, which
    is what keeps the gate vocabulary-blind and what makes "the operator's
    blr/pune/hyd is reproduced exactly" true by inspection rather than by
    argument. The country floor belongs to RELAXATION — it is the thing that stops
    "I do not mind where" meaning "including Berlin".

    CITY_SELECTION_NEVER_MATCHES IS THE ONE PLACE THIS GATE KNOWS A LABEL BY NAME,
    and it is checked before the membership test rather than filtered out of the
    list, so a row edited by hand in psql cannot get past it either. See that
    constant for why india-unclear and non-india are on it and other-india is not.

    WHAT THIS STILL DELIBERATELY DOES NOT READ is preferences.work_modes. That
    column now exists and now decides — work_mode_ok is where it decides — and
    keeping it out of THIS function is what stops one rule from carrying two
    arguments. This branch says "a remote posting has no city to be tested
    against", which is a statement about geography and is true whatever anybody
    ticked. Whether that person WANTS a remote job is the other test, and it runs
    beside this one in title_stage_ok rather than inside it.

    The two do interact, and the interaction is the right way round: an account
    that did not tick `remote` never sees a remote posting at all, so the
    relaxation cannot smuggle one past a city gate for somebody who did not ask
    for it. That falls out of the AND rather than needing a rule.
    """
    city = job.get("city") or ""

    if is_remote(job) or prefs.all_india:
        return city != NON_INDIA_LABEL

    if city in CITY_SELECTION_NEVER_MATCHES:
        return False

    return city in prefs.effective_cities


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
    """The FIVE free tests: city AND work mode AND keyword AND NOT excluded AND
    NOT level.

    "Free" is what the name has always meant, rather than "about the title" —
    these are the tests that read a column the crawler already stored, against a
    job that has cost no network request. work_mode joined them in the work-mode
    slice and belongs here on exactly that basis: crawler/store.upsert_seen does
    not write it, but notify/description.py does, so by the time this runs the
    answer is a string on the row or a NULL that reads as onsite. No fetch.

    IT IS NO LONGER THE ENRICHMENT SEAM ON ITS OWN — see worth_a_description
    below, which is wider by one branch and is what crawler/enrich.py calls. This
    function decides what MATCHES; that one decides what is worth a request, and
    conflating them made the remote branch unable to fire.
    """
    return (
        city_ok(job, prefs)
        and work_mode_ok(job, prefs)
        and keyword_ok(job, prefs)
        and not excluded(job, prefs)
        and not level_excluded(job, prefs)
    )


def worth_a_description(job: Mapping[str, Any], prefs: Preferences) -> bool:
    """Is this job worth spending a network request on? The enrichment seam.

    WIDER THAN title_stage_ok BY EXACTLY ONE BRANCH, and without that branch the
    remote relaxation could never fire on a single row. The circularity is worth
    spelling out because it is not obvious and it is fatal:

        a job that remote-relaxes-city would admit is, by definition, a job that
        FAILS city_ok today. Narrow the enrichment queue by title_stage_ok and
        such a job never earns a description read, so work_mode stays NULL, so it
        never becomes 'remote', so the relaxation never fires. The feature would
        ship green and admit nothing, forever.

    So a row that fails ONLY the city gate is still worth asking about — but only
    if a remote verdict could actually rescue it, and that is where the cost stays
    bounded. india_funnel is the country floor, the same one city_ok applies to
    its relaxing branches: a `non-india` posting can never match whatever its
    arrangement turns out to be, so it is never worth a request. On the live board
    that is the difference between 390 extra candidates and 24.

    IT READS THE LOCATION STRING RATHER THAN jobs.city, deliberately. The stored
    label is whatever the labeller of the day wrote; the string is the evidence,
    and fetchers.normalize is the current reading of it. That keeps this seam
    correct on a database whose city labels predate the current vocabulary, which
    is precisely the state this one is in.

    work_mode_ok is NOT applied here, and that is the one asymmetry. A job's mode
    is the thing the fetch exists to discover, so filtering on it first would be
    asking the answer to decide whether to ask the question.
    """
    if not (
        keyword_ok(job, prefs)
        and not excluded(job, prefs)
        and not level_excluded(job, prefs)
    ):
        return False
    if city_ok(job, prefs):
        return True
    return india_funnel(job.get("location"))


def pm_excluded(job: Mapping[str, Any]) -> bool:
    """The stored is_pm_role flag. Computed by notify.description.detect_pm_role.

    Read, never recomputed — the same contract as `city`. The evidence was the
    description text, which no longer exists anywhere; the conclusion is the
    column. An unenriched row is FALSE, which means kept.
    """
    return bool(job.get("is_pm_role"))


def experience_excluded(job: Mapping[str, Any], prefs: Preferences) -> bool:
    """The stated minimum is outside this account's looking range.

    THREE BRANCHES, AND EACH ANSWERS A DIFFERENT QUESTION.

    THE ORDER IS LOAD BEARING AND WAS WRONG IN THE FIRST DRAFT. The unconfigured
    check has to come FIRST, before the NULL-min_experience one. Written the other
    way round, an account with no range but the toggle at TRUE keeps every job whose
    description states no minimum — which on the live database is 7,809 of 7,880
    open rows, so "matches nothing" would have quietly meant "matches almost
    everything". It was caught by a test rather than by review, and by the fact that
    dashboard/src/lib/jobs.experienceClause had the order right: the SQL prefilter
    would have excluded rows this function kept, making the prefilter narrower than
    the rule it prefilters for, which is the one thing that file may not be.

    1. An unconfigured range -> EXCLUDE EVERYTHING. Deliberately not a fallback to
       (0, 2), and the reasoning is the one Slices 1b and 1c settled: a filter
       nobody chose, applied silently, is the expensive failure. It cannot widen
       into somebody's inbox and it cannot narrow a filter somebody tuned, because
       there is no filter here to narrow.

       IT IS ALSO UNREACHABLE FOR AN ONBOARDED ACCOUNT, by construction rather
       than by luck: PUT /api/preferences refuses a save whose range is incomplete
       and is the only writer of these columns AND of onboarding_completed, so
       "onboarded" now implies "configured". The one row in this state today is the
       account that has never finished onboarding, which lib/guard.requirePage
       keeps off every screen that computes a match and db/users.DIGEST_USERS_SQL
       keeps out of the fan-out (is_active FALSE). npm run onboarding-check asserts
       the implication against the live database.

       Excluding rather than RAISING is a decision about blast radius.
       notify/digest.run_fan_out catches DigestError and nothing else, so an
       exception raised here would take every account's digest down over one
       account's unconfigured row — the "one broken account silences every other"
       failure the fan-out is arranged against.

    2. NULL min_experience -> the account decides. Silence still covers the same
       three things (fetch failed, no minimum stated, nobody has read it yet) and
       still means "we do not know"; show_no_experience_jobs is the answer to what
       to do about not knowing. TRUE keeps it, which is what this install has
       always done and what its row says; FALSE hides it. A non-integer value is
       silence too, rather than an exception in the middle of a digest run.

    3. A confident number -> range membership, ends INCLUSIVE. This is where the
       junior half lives: a minimum BELOW the range drops the posting exactly as a
       minimum above it does. Written as a range compare rather than by building
       the integer set the spec describes, because for integers the two are the
       same test, and this is the form dashboard/src/lib/jobs.ts can state to
       Postgres verbatim as BETWEEN — a rule the two runtimes must agree on is
       worth writing in the shape both can express.
    """
    low, high = prefs.exp_range_low, prefs.exp_range_high
    if low is None or high is None:
        return True

    years = min_experience_of(job)
    if years is None:
        return not prefs.show_no_experience_jobs

    return not (low <= years <= high)


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
    """The title stage AND NOT a PM role AND NOT a 3+-years role AND priced OK.

    Every exclusion is checked after the inclusions and each wins outright:
    "Senior Backend Engineer, Bengaluru", "SDE III, Bengaluru" and Razorpay's
    "Full Stack Builder" (a technical-PM role asking for 3-6 years) all pass the
    tests before the one that drops them. The point of the filter is a small
    inbox, not a complete one.

    They are separate tests rather than one because they fail on different kinds
    of evidence — a word in the title, a number in the title, a fact from the
    description, another fact from the description, a figure from the description
    — and a preview run has to be able to say which.

    SALARY IS HERE AND NOT IN title_stage_ok, DELIBERATELY. It is a
    description-derived fact like is_pm_role and min_experience, and the title
    stage is what worth_a_description narrows the enrichment queue by — so a pay
    test up there would be asking the answer to decide whether to ask the
    question, the asymmetry that function's docstring already names for
    work_mode_ok. Keeping it here costs nothing, because an unpriced job passes
    the gate anyway and every candidate is unpriced until it has been read.
    """
    return (
        title_stage_ok(job, prefs)
        and not pm_excluded(job)
        and not experience_excluded(job, prefs)
        and salary_ok(job, prefs)
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
        # THREE WAYS TO FAIL ONE GATE, worded so the report names the branch that
        # actually fired. Same discipline as the experience reasons below, and the
        # same constraint: dashboard/src/lib/match.explain states these strings
        # character for character, because match-parity prints them when the two
        # languages disagree and a reason that reads differently makes a real
        # drift harder to see than no reason at all.
        city = job.get("city") or ""
        if prefs.all_india or is_remote(job):
            reason = f"all-India, but {job.get('city')!r} is not in India"
        elif city in CITY_SELECTION_NEVER_MATCHES:
            reason = (
                f"city {job.get('city')!r} never matches an explicit city "
                f"selection — it is covered by the all-India switch"
            )
        else:
            reason = f"city {job.get('city')!r} not in preferences"
    elif not work_mode_ok(job, prefs):
        # Two ways to fail one test, and the second is worth naming separately
        # because it is a blank filter rather than a rejected job. Stated
        # character for character in dashboard/src/lib/match.explain.
        if not prefs.work_modes:
            reason = "no work arrangement is selected"
        else:
            reason = (
                f"work mode {work_mode_of(job)!r} is not one of "
                f"{', '.join(repr(m) for m in prefs.work_modes)}"
            )
    elif not keywords_hit:
        reason = "no keyword in title"
    elif exclusions_hit:
        reason = "excluded by " + ", ".join(repr(t) for t in exclusions_hit)
    elif level is not None and level > prefs.max_level:
        reason = f"level {level} above max_level {prefs.max_level}"
    elif is_pm:
        reason = "description frames this as a PM/management role"
    elif experience_excluded(job, prefs):
        # One test, four ways to fail it — and a preview that said only "outside
        # the range" would leave the reader to work out which, when the whole
        # point of this function is that it does not make them.
        # Same order as the test itself, so the explanation names the branch that
        # actually fired rather than the first one a reader would guess.
        low, high = prefs.exp_range_low, prefs.exp_range_high
        if low is None or high is None:
            reason = "the looking range is not configured"
        elif min_experience is None:
            reason = "experience unknown, and this account hides jobs stating no minimum"
        elif min_experience < low:
            reason = f"minimum {min_experience} years below the looking range {low}-{high}"
        else:
            reason = f"minimum {min_experience} years above the looking range {low}-{high}"
    elif not salary_ok(job, prefs):
        # ONE WAY TO FAIL THIS ONE, unlike the tests above, and that is worth
        # saying rather than leaving the single branch to look like an oversight.
        # Both of salary_ok's other branches RETURN TRUE, so a job that reaches
        # here is necessarily priced and necessarily outside every ticked band.
        # Stated character for character in dashboard/src/lib/match.explain.
        low, high = salary_of(job)
        stated = f"{low}-{high} LPA" if low != high else f"{low} LPA"
        reason = (
            f"pay {stated} is outside the selected band(s) "
            f"{', '.join(repr(b) for b in prefs.salary_bands)}"
        )
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

    Candidacy — open, watched by this person, not back catalogue, not already
    sent to them — is enforced by the SELECT in notify/store.py, not here,
    because those are facts about the row's lifecycle and about who is asking,
    rather than about whether the job is interesting.
    """
    return [job for job in jobs if is_match(job, prefs)]
