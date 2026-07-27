"""Turn a company NAME into a tracked company, or into a logged near-miss.

One candidate goes in; one verdict comes out. The verdict is decided by two
bars and nothing else:

    (a) the name resolves to a board on one of the six Tier-1 ATSes, and
    (b) that board has at least one open job labelled blr, hyd or pune.

Clear both and the company is upserted into `companies` with tracked = TRUE.
Clear the first but not the second and it is logged to `discovered_companies`
with promoted = FALSE — visible, not watched. Clear neither and it is still
logged, because "we tried this name and no board answered" is worth exactly as
much the second time as the first: it is what stops the next run spending thirty
requests on it again.

WHY THE CITY BAR IS CHECKED BEFORE TRACKING RATHER THAN AFTER
-------------------------------------------------------------
The crawler stores every job on every tracked board, in every city, forever —
store loosely, notify tightly. That is the right rule for a company someone
chose to watch, and the wrong rule for a company an automated sweep happened to
find: a hundred boards with no Indian presence would be a hundred boards fetched
every hour, and several thousand rows of San Francisco job history, to filter
all of it out at the last step. So the sweep applies the city test once, at the
door, against the board it has just fetched. It is the only filtering decision
in this project that happens before storage, and it is about which BOARDS are
worth an hourly request — never about which jobs are worth showing.

WHAT THIS MODULE MAY WRITE
--------------------------
`companies` and `discovered_companies`. Never `jobs`. A newly-tracked company is
picked up by the next scheduled scripts/crawl.py, which sees zero stored rows
for it, and therefore seeds its whole back catalogue with notified = TRUE — the
same silent seed every other company got. Writing job rows here would break that
detection and turn a discovery run into an inbox full of a stranger's back
catalogue.

And the upsert into `companies` is ON CONFLICT DO NOTHING, never DO UPDATE. A
row that is already there was either added by hand or tracked by an earlier run,
and in both cases it knows things this sweep does not: a corrected name, a
hand-tagged category, a Workday config, or the fact that a person deliberately
set tracked = FALSE. A sweep that re-tracked those would be a sweep that undoes
a decision every time it runs.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Iterable, Sequence

from fetchers.normalize import city_label, iso_or_none, norm
from fetchers.resolve import DISCOVERY_ORDER, probe_boards
from fetchers.tier1 import fetch as fetch_tier1

# The three cities the whole tool is scoped to. A board with none of these is
# not tracked, however alive it is.
TARGET_CITIES: tuple[str, ...] = ("blr", "hyd", "pune")

# Verdicts, in the order the report prints them.
TRACKED = "tracked"  # cleared both bars, now in companies
ALREADY = "already-tracked"  # cleared both bars, but companies already had the row
NO_CITY = "no-city"  # resolved to a real board with nothing in the three cities
UNRESOLVED = "unresolved"  # no Tier-1 board answered for any token variant
FAILED = "failed"  # the probe itself blew up; nothing was learned

# `ats` and `token` are NOT NULL on discovered_companies, so an unresolved
# candidate still needs a pair. This is the placeholder that says "this row is a
# name we could not turn into a board" — it can never collide with a real ATS.
UNRESOLVED_ATS = "unresolved"

# Name-shape heuristic for the discovered_companies.looks_like_services flag.
# It is recorded, never acted on: a staffing firm with real Bangalore engineering
# reqs still clears both bars, and the flag is there so a person reviewing the
# table can see why a board is full of client-site postings.
_SERVICES_MARKERS = (
    "consulting", "consultancy", "consultants", "services", "solutions",
    "staffing", "outsourcing", "outsource", "manpower", "recruitment",
    "recruiters", "resourcing", "placements", "infotech", "technologies pvt",
    "software pvt", "it services",
)


def looks_like_services(name: str) -> bool:
    """Does the NAME read like a services/staffing firm? A hint, not a verdict."""
    text = f" {norm(name)} "
    return any(re.search(rf"\b{re.escape(marker)}\b", text) for marker in _SERVICES_MARKERS)


def city_counts(jobs: Iterable[dict]) -> dict[str, int]:
    """{city label: how many of these jobs carry it}, using the stored labeller.

    Deliberately the SAME fetchers.normalize.city_label the crawler will apply
    when it stores these rows an hour later. If the two disagreed, a company
    could be tracked for Bangalore jobs that then stored as `other`.
    """
    counts: dict[str, int] = {}
    for job in jobs:
        label = city_label(job.get("location"))
        counts[label] = counts.get(label, 0) + 1
    return counts


def city_hits(counts: dict[str, int]) -> list[str]:
    """Which of the three target cities actually appeared, in a stable order."""
    return [city for city in TARGET_CITIES if counts.get(city, 0) > 0]


def _latest_posting(jobs: Iterable[dict]) -> datetime | None:
    """The newest vendor posted_at across a board, as an aware datetime.

    Advisory, exactly as it is everywhere else in this project: it goes into
    discovered_companies.last_posting_at so a person can see at a glance whether
    a board is live or abandoned. Nothing decides anything from it.
    """
    best: datetime | None = None
    for job in jobs:
        text = iso_or_none(job.get("posted_at"))
        if not text:
            continue
        try:
            stamp = datetime.fromisoformat(text)
        except ValueError:
            continue
        if best is None or stamp > best:
            best = stamp
    return best


@dataclass(frozen=True)
class Candidate:
    """A name to try, and whatever the source already knew about it.

    `ats`/`token` come from a source that read an actual careers URL (GitHub
    README mining), and turn a thirty-request search into a one-request check.
    `enterprise` comes from the seed list's GCC section and only ever changes
    the note on an unresolved row.
    """

    name: str
    source: str = "seed"
    ats: str | None = None
    token: str | None = None
    enterprise: bool = False
    note: str | None = None

    @property
    def key(self) -> str:
        """The identity used for de-duplication and for resume-skipping."""
        return norm(self.name)


@dataclass
class Outcome:
    """What one candidate turned out to be."""

    candidate: Candidate
    verdict: str
    ats: str | None = None
    token: str | None = None
    jobs_seen: int = 0
    cities: dict[str, int] = field(default_factory=dict)
    last_posting_at: datetime | None = None
    note: str | None = None
    error: str | None = None

    @property
    def hits(self) -> list[str]:
        return city_hits(self.cities)

    @property
    def in_city_jobs(self) -> int:
        return sum(self.cities.get(city, 0) for city in TARGET_CITIES)


async def probe(candidate: Candidate, *, max_variants: int | None = None) -> Outcome:
    """Resolve a candidate and read its board. Network only — writes nothing.

    Never raises. A candidate whose probe throws comes back as FAILED with the
    exception recorded, for the same reason Guard 1 exists in crawler/loop.py: a
    single unreachable vendor must not end a sweep of two hundred names.
    """
    try:
        if candidate.ats and candidate.token:
            # The source read a real careers URL, so there is nothing to guess.
            # One request settles it — and an empty answer is a real answer,
            # exactly as it is in resolve(): several vendors return 200 with an
            # empty list for tokens that do not exist.
            jobs = await fetch_tier1(candidate.ats, candidate.token, save_raw=False)
            found = (candidate.ats, candidate.token, jobs) if jobs else None
        else:
            found = await probe_boards(
                candidate.name, order=DISCOVERY_ORDER, max_variants=max_variants
            )
    except Exception as exc:  # noqa: BLE001 — one bad board must not end the sweep
        return Outcome(
            candidate=candidate,
            verdict=FAILED,
            error=f"{type(exc).__name__}: {exc}",
            note=candidate.note,
        )

    if found is None:
        note = candidate.note or ("try Workday" if candidate.enterprise else None)
        return Outcome(candidate=candidate, verdict=UNRESOLVED, note=note)

    ats, token, jobs = found
    counts = city_counts(jobs)
    verdict = TRACKED if city_hits(counts) else NO_CITY
    return Outcome(
        candidate=candidate,
        verdict=verdict,
        ats=ats,
        token=token,
        jobs_seen=len(jobs),
        cities=counts,
        last_posting_at=_latest_posting(jobs),
        note=candidate.note,
    )


# --- the two statements this module is allowed to run ----------------------

# DO NOTHING, not DO UPDATE. See the module docstring: an existing row knows
# more than this sweep does, including the possibility that a person turned
# tracking off on purpose. RETURNING therefore yields a row only on a genuine
# insert, which is exactly the "newly tracked" count the report wants.
#
# india_scoped is FALSE by construction rather than by choice. The flag means
# "these rows are deliberately partial because the board was filtered at
# source", and only Workday does that — every board this statement can ever
# insert is a Tier-1 board fetched whole. category is 'unknown' for the honest
# reason: nothing here knows whether a company is a product firm or a GCC, and
# a guess would be indistinguishable from the hand-tagging it would overwrite.
TRACK_SQL = """
INSERT INTO companies (name, ats, token, config, category, tracked, india_scoped)
VALUES ($1, $2, $3, '{}'::jsonb, 'unknown', TRUE, FALSE)
ON CONFLICT (ats, token) DO NOTHING
RETURNING id
"""

# The staging log. Re-running a sweep refreshes what a board looks like now
# (job count, city hits, freshness) but never un-promotes a row: `promoted` is
# OR-ed rather than assigned, so a company promoted by hand between two runs
# stays promoted even if its board has gone quiet in the meantime.
LOG_SQL = """
INSERT INTO discovered_companies (name, ats, token, config, job_count,
                                  last_posting_at, city_hits,
                                  looks_like_services, source, promoted)
VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7, $8, $9, $10)
ON CONFLICT (ats, token) DO UPDATE
   SET name                = EXCLUDED.name,
       config              = EXCLUDED.config,
       job_count           = EXCLUDED.job_count,
       last_posting_at     = EXCLUDED.last_posting_at,
       city_hits           = EXCLUDED.city_hits,
       looks_like_services = EXCLUDED.looks_like_services,
       source              = EXCLUDED.source,
       promoted            = discovered_companies.promoted OR EXCLUDED.promoted
"""


def log_row(outcome: Outcome) -> tuple:
    """The discovered_companies parameter tuple for one outcome.

    An unresolved candidate has no (ats, token) and the column pair is NOT NULL,
    so it is logged under the UNRESOLVED_ATS placeholder keyed by its normalised
    name. That is what makes a second run skip it instead of re-probing it.
    """
    candidate = outcome.candidate
    ats = outcome.ats or UNRESOLVED_ATS
    token = outcome.token or (candidate.key.replace(" ", "") or "unknown")

    config: dict[str, Any] = {"candidate": candidate.name}
    if outcome.note:
        config["note"] = outcome.note
    if outcome.error:
        config["error"] = outcome.error
    if outcome.cities:
        config["cities"] = outcome.cities

    return (
        candidate.name,
        ats,
        token,
        json.dumps(config),
        outcome.jobs_seen or None,
        outcome.last_posting_at,
        outcome.hits,
        looks_like_services(candidate.name),
        candidate.source,
        outcome.verdict in (TRACKED, ALREADY),
    )


async def record(conn, outcome: Outcome) -> Outcome:
    """Write one outcome: track it if it earned it, and log it either way.

    Returns the outcome, with TRACKED downgraded to ALREADY when the companies
    row already existed — the insert declined, so nothing was written there and
    the report must not claim a new company.
    """
    if outcome.verdict == TRACKED:
        company_id = await conn.fetchval(
            TRACK_SQL, outcome.candidate.name, outcome.ats, outcome.token
        )
        if company_id is None:
            outcome.verdict = ALREADY

    await conn.execute(LOG_SQL, *log_row(outcome))
    return outcome


# --- resume support --------------------------------------------------------


@dataclass
class Known:
    """What a previous run (or a person) has already settled.

    Two questions, both answered without a network request: have we already got
    this company, and have we already tried this name?
    """

    company_names: set[str] = field(default_factory=set)
    company_boards: set[tuple[str, str]] = field(default_factory=set)
    logged_names: set[str] = field(default_factory=set)
    logged_boards: set[tuple[str, str]] = field(default_factory=set)

    def seen(self, candidate: Candidate) -> str | None:
        """Why this candidate should be skipped, or None to probe it.

        Matching is on the normalised NAME, plus the (ats, token) pair when the
        source supplied one. Name-matching is what makes the sweep resumable
        after an interrupt: the seed list is names, so a name already logged is
        a name already spent.
        """
        key = candidate.key
        if key in self.company_names:
            return "already a tracked company"
        if candidate.ats and candidate.token:
            board = (candidate.ats, candidate.token)
            if board in self.company_boards:
                return "board already in companies"
            if board in self.logged_boards:
                return "board already logged"
        if key in self.logged_names:
            return "already logged this run or a previous one"
        return None


async def load_known(conn) -> Known:
    """Read the two tables a sweep must not duplicate work against."""
    known = Known()
    for row in await conn.fetch("SELECT name, ats, token FROM companies"):
        known.company_names.add(norm(row["name"]))
        known.company_names.add(norm(row["token"]))
        known.company_boards.add((row["ats"], row["token"]))
    for row in await conn.fetch("SELECT name, ats, token FROM discovered_companies"):
        if row["name"]:
            known.logged_names.add(norm(row["name"]))
        known.logged_boards.add((row["ats"], row["token"]))
    return known


def dedupe(candidates: Sequence[Candidate]) -> list[Candidate]:
    """Drop repeats, keeping first-seen order and the best of what each knew.

    A name can arrive from the seed list AND from GitHub with a board attached.
    The two halves are merged rather than one replacing the other: the FIRST
    entry keeps the name (a person typed "Rolls-Royce"; GitHub mining would call
    the same company "rollsroyce", and the name is what a companies row shows),
    while the board hint is adopted from whichever entry has one, because it
    turns a thirty-request search into a single GET.
    """
    chosen: dict[str, Candidate] = {}
    order: list[str] = []
    for candidate in candidates:
        key = candidate.key
        if not key:
            continue
        existing = chosen.get(key)
        if existing is None:
            chosen[key] = candidate
            order.append(key)
        elif existing.token is None and candidate.token is not None:
            chosen[key] = replace(
                existing,
                ats=candidate.ats,
                token=candidate.token,
                source=f"{existing.source}+{candidate.source}",
            )
    return [chosen[key] for key in order]
