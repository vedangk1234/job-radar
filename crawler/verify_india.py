"""Ask one already-harvested board a single question: does it hire in India?

One board goes in; one verdict comes out. This is crawler/discover.py's two-bar
test with the first bar already cleared and the second one widened:

    (a) the board's (ats, token) is known — the Common Crawl harvester read it
        out of a real URL, so there is nothing to resolve and nothing to guess,
    (b) the board has at least one open job that labels as anything other than
        `non-india`.

THE BAR IS DELIBERATELY LOOSE, AND THE VERDICT STORES BOTH NUMBERS
-------------------------------------------------------------------
Bar (b) is `fetchers.normalize.india_funnel` — the complement of `non-india` —
so `india-unclear` counts as India. That is the right bar for the question this
run answers ("which boards are worth looking at later"), and it is a MEASURABLY
noisy one on this population. Measured on 30 harvested boards before this module
existed: a hotel in Eemnes scored 10 India jobs out of 10, because its postings
read "Eemnes, Utrecht, Nederland" and `nederland` is not in the non-India table
while `netherlands` is. `india-unclear` means "probably India" on the fifty
hand-picked Indian boards it was written for, and it inverts on eleven thousand
random global ones.

So every verdict stores BOTH numbers, from the same histogram, at no extra cost:

    india_loose = jobs - non_india          the stored bar (india-unclear counts)
    india_named = jobs - non_india - india_unclear
                                            a CONFIDENT India statement: one of
                                            the 23 cities, or other-india

The gap between them is the size of the inflation, per board and in total, and
the promotion task that comes next can pick either without re-probing anything.
Fixing the marker gap is a different task; this run produces the evidence for it
(see `unclear_sample`, and the residue table the sweep prints).

WHAT THIS MODULE MAY WRITE
--------------------------
`discovered_companies`, and nothing else, through the one UPDATE below. It never
writes `companies`, never writes `jobs`, never writes `user_companies`, and
never touches `promoted`. This run PROMOTES NOTHING: it is a measurement, and
the decision it feeds has not been taken yet. `VERIFY_SQL` is an UPDATE keyed by
id rather than an upsert, so it cannot even create a row — it can only fill in
the columns of one the harvester already wrote. tests/test_verify_india.py
asserts that against the statement TEXT, the same way tests/test_commoncrawl.py
does for the harvester.

WHY NOT crawler.discover.record
-------------------------------
One line. `discover.log_row` writes `outcome.jobs_seen or None`, which stores
NULL for a board that answered with zero jobs. That is correct there — NULL
means "nobody has looked at this board", which is what the harvester leaves
behind — and it is fatal here: it would make a probed-and-dead board
indistinguishable from a never-probed one, and every resume would re-fetch the
thousands of dead boards it had already paid for. This module writes 0 as 0, so
the three states stay distinguishable:

    job_count IS NULL      never probed
    job_count = 0          probed, and the board gave us nothing
    job_count > 0          probed, and it answered

Everything else is reused rather than restated: fetchers.tier1.fetch is the same
fetcher the crawler uses, and crawler.discover.city_counts / latest_posting are
the same labeller and the same freshness reading, so this sweep and the hourly
crawl cannot disagree about what a board says.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from crawler.discover import city_counts, latest_posting
from fetchers.normalize import city_label
from fetchers.tier1 import fetch as fetch_tier1

# The two labels that are not a confident statement that a job is in an Indian
# place. Named here rather than inlined because the whole point of this module
# is the distinction between them.
NON_INDIA = "non-india"
UNCLEAR = "india-unclear"

# Verdicts, in the order the report prints them. They live in
# discovered_companies.config->>'probe'.
INDIA = "india"  # >= 1 job, >= 1 of them not non-india    <- the number we want
NO_INDIA = "no-india"  # >= 1 job, every one of them non-india
EMPTY = "empty"  # the board answered with no jobs at all
ERROR = "error"  # the fetch raised; nothing was learned

# The verdicts that mean "we reached this board and it had postings".
REACHED = (INDIA, NO_INDIA)

# SmartRecruiters is the only paginated Tier-1 board, and this run does not need
# all 5,000 jobs of a 5,000-job tenant to answer "does it hire in India, and
# roughly how much" — the top bucket of the distribution is "20+". Ten pages is
# 1,000 jobs, far past that boundary, and it bounds the one lane that can
# otherwise stall: aecom2 was measured at 4,939 jobs / 50 requests / 50 seconds
# in a single board. A board that hits the cap is flagged `truncated`, so its
# count is never silently understated.
SR_MAX_PAGES = 10

# fetch_smartrecruiters pages at 100. Its loop breaks early on any short page,
# so an exact multiple of the page size is the signal that the cap stopped it.
SR_PAGE_SIZE = 100


@dataclass(frozen=True)
class Board:
    """One harvested row, as the sweep selects it.

    `config` is what is already stored — the harvest provenance keys, which a
    probe must preserve rather than overwrite.
    """

    id: int
    ats: str
    token: str
    config: dict[str, Any] = field(default_factory=dict)


@dataclass
class Verdict:
    """What one board turned out to be."""

    board: Board
    probe: str
    jobs: int = 0
    cities: dict[str, int] = field(default_factory=dict)
    last_posting_at: datetime | None = None
    truncated: bool = False
    error: str | None = None
    unclear_sample: str | None = None

    # --- the two numbers, both derived from the one histogram ---------------

    @property
    def non_india(self) -> int:
        return self.cities.get(NON_INDIA, 0)

    @property
    def india_unclear(self) -> int:
        return self.cities.get(UNCLEAR, 0)

    @property
    def india_loose(self) -> int:
        """The stored bar: every job that is not `non-india`.

        Identical by construction to counting the jobs for which
        fetchers.normalize.india_funnel answers True — that function is defined
        as the same complement, and tests/test_verify_india.py proves the two
        agree rather than assuming it.
        """
        return self.jobs - self.non_india

    @property
    def india_named(self) -> int:
        """The confident number: one of the 23 cities, or other-india."""
        return self.jobs - self.non_india - self.india_unclear

    @property
    def india_labels(self) -> list[str]:
        """Which India labels appeared, sorted. This is what city_hits stores.

        Wider than crawler.discover.city_hits, which answers "which of blr, hyd,
        pune" — this run's question is "which India labels at all", so the array
        holds the 23 cities, other-india and india-unclear. Nothing outside
        crawler/discover.py and the tests reads the column.
        """
        return sorted(label for label in self.cities if label != NON_INDIA)


def commonest_unclear(jobs: list[dict]) -> str | None:
    """The location string that produced india-unclear most often on this board.

    One short string per row is what makes the residue — "which location strings
    is the labeller failing to place?" — reconstructible from SQL later, without
    re-fetching eleven thousand boards. Ties break on the string itself so the
    answer is stable rather than dictionary-order.
    """
    tally: dict[str, int] = {}
    for job in jobs:
        location = job.get("location")
        if city_label(location) == UNCLEAR:
            text = str(location or "").strip() or "<empty>"
            tally[text] = tally.get(text, 0) + 1
    if not tally:
        return None
    return max(tally, key=lambda text: (tally[text], text))


async def probe_board(board: Board, *, sr_max_pages: int = SR_MAX_PAGES) -> Verdict:
    """Fetch one board and label its jobs. Network only — writes nothing.

    Never raises. A board whose fetch throws comes back as ERROR with the
    exception recorded, for the same reason crawler/discover.probe does it: one
    unreachable vendor must not end a sweep of eleven thousand boards.
    """
    try:
        jobs = await fetch_tier1(
            board.ats, board.token, save_raw=False, max_pages=sr_max_pages
        )
    except Exception as exc:  # noqa: BLE001 — one bad board must not end the sweep
        return Verdict(board=board, probe=ERROR, error=f"{type(exc).__name__}: {exc}")

    if not jobs:
        # The fetcher returns [] for a 404, for a 200-with-an-empty-array, and
        # for exhausted retries alike, so this bucket honestly folds "delisted
        # board" together with "live board, no openings". Separating them means
        # going below fetchers/tier1.py, which is not what this run is for.
        return Verdict(board=board, probe=EMPTY)

    counts = city_counts(jobs)
    return Verdict(
        board=board,
        probe=INDIA if len(jobs) > counts.get(NON_INDIA, 0) else NO_INDIA,
        jobs=len(jobs),
        cities=counts,
        last_posting_at=latest_posting(jobs),
        truncated=(
            board.ats == "smartrecruiters" and len(jobs) == sr_max_pages * SR_PAGE_SIZE
        ),
        unclear_sample=commonest_unclear(jobs),
    )


# --- the one statement this module is allowed to run -----------------------

# UPDATE, keyed by id. Not an upsert, not an INSERT: this run measures rows the
# harvester already wrote and must not be able to invent one. It names exactly
# one table, and `companies`, `jobs`, `user_companies` and `promoted` do not
# appear in it at all — so "this run promotes nothing" is a property of the
# statement rather than a promise the caller has to keep.
#
# `config` is assigned, not merged with `||`. The harvest keys (harvest,
# crawl_id, host, india_hint) are preserved in Python by probe_config, which
# reads the config the batch SELECT already returned. Two reasons: `||` would
# make the preservation claim untestable on the SQLite mirror, where `||` is
# string concatenation rather than a jsonb merge, and nothing else writes this
# column, so there is no concurrent writer for a merge to protect against.
VERIFY_SQL = """
UPDATE discovered_companies
   SET job_count       = $2,
       city_hits       = $3,
       last_posting_at = $4,
       config          = $5::jsonb
 WHERE id = $1
"""

# The rows this run is for: harvested, and never looked at. `job_count IS NULL`
# is the resume predicate — it is what the harvester leaves behind and what this
# module fills in, so an interrupted sweep re-selects exactly what it did not
# reach. Ordering is round-robin across ats, done in Python by the sweep, not
# here: the lanes are what the whole politeness design is built on.
SELECT_UNPROBED_SQL = """
SELECT id, ats, token, config
  FROM discovered_companies
 WHERE source = $1
   AND job_count IS NULL
 ORDER BY id
"""

# --retry-errors: the boards whose fetch threw. They carry job_count = 0 like
# any other dead board, so the ordinary resume leaves them alone — retrying a
# transport error forever is how a sweep never finishes.
SELECT_ERRORS_SQL = """
SELECT id, ats, token, config
  FROM discovered_companies
 WHERE source = $1
   AND config->>'probe' = 'error'
 ORDER BY id
"""


def probe_config(verdict: Verdict) -> dict[str, Any]:
    """The board's existing config, plus what this probe learned.

    The harvest keys are carried through rather than replaced: a row's
    provenance (which crawl found it, on which host) is not something a probe is
    entitled to forget.
    """
    config = dict(verdict.board.config)
    config.update(
        {
            "probe": verdict.probe,
            "probed_at": datetime.now(timezone.utc).isoformat(),
            "jobs": verdict.jobs,
            "india_jobs": verdict.india_loose,
            "india_named": verdict.india_named,
            "india_unclear": verdict.india_unclear,
            "non_india": verdict.non_india,
        }
    )
    if verdict.cities:
        config["cities"] = verdict.cities
    if verdict.unclear_sample:
        config["unclear_sample"] = verdict.unclear_sample
    if verdict.truncated:
        config["truncated"] = True
    if verdict.error:
        config["probe_error"] = verdict.error
    else:
        # A retry that succeeds must not leave the old failure behind still
        # claiming this board is broken.
        config.pop("probe_error", None)
    return config


def verify_row(verdict: Verdict) -> tuple:
    """The VERIFY_SQL parameter tuple for one verdict.

    job_count is `verdict.jobs` and never `jobs or None`: 0 is a measurement
    here, and writing NULL for it would put the row straight back into the
    never-probed pool on the next run. See the module docstring.
    """
    return (
        verdict.board.id,
        verdict.jobs,
        verdict.india_labels,
        verdict.last_posting_at,
        json.dumps(probe_config(verdict)),
    )


async def record(conn, verdict: Verdict) -> Verdict:
    """Write one verdict. One statement, one table, one row."""
    await conn.execute(VERIFY_SQL, *verify_row(verdict))
    return verdict
