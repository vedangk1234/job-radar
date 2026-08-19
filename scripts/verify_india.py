"""Probe every harvested board once and measure how many of them hire in India.

    python scripts/verify_india.py --limit 300      # the calibration run, ~5 min
    python scripts/verify_india.py                  # the full sweep, ~75-110 min
    python scripts/verify_india.py --report-only    # re-read the report, no network
    python scripts/verify_india.py --retry-errors   # re-probe only the rows that threw
    python scripts/verify_india.py --dry-run        # probe and report, write nothing

MEASUREMENT ONLY. It fills in job_count, city_hits, last_posting_at and a probe
verdict on `discovered_companies` rows the Common Crawl harvest already wrote,
and that is all it does. It promotes nothing, writes no `companies` row, writes
no `jobs` row, and is not part of the hourly crawl. The single question it
answers is the one that replaces every estimate: of the 11,435 harvested boards,
how many actually hire in India? See crawler/verify_india.py for the verdict and
the one statement; this file is the sweep around it, exactly as
scripts/run_discovery.py is the sweep around crawler/discover.py.

THE LANES, AND WHY THE ORDER OF THE WORK DECIDES THE RUNTIME
------------------------------------------------------------
Every board here is one request to one KNOWN host — there is nothing to resolve,
so nothing drifts. That makes this sweep vulnerable to a failure
scripts/run_discovery.py escapes by accident, and it is worth being precise
about because it is the difference between 72 minutes and 3 hours 20.

fetchers/http.py takes the global 8-slot semaphore BEFORE it waits out a host's
one-request-a-second pace, so a coroutine sitting on a host lock is holding one
of the eight slots. Feed the work in table order — 4,294 Greenhouse boards, then
3,145 Workable — and eight Greenhouse tasks take every slot and queue behind one
lock. Five hosts, one of them working, and the whole sweep serialises onto a
single lane: ~12,100 seconds.

So the work is split into one QUEUE PER ATS, and the number of workers is
allocated so that the total is exactly MAX_CONCURRENCY. Every worker therefore
always holds a slot, no worker ever blocks on the semaphore, and each lane's
rate is set by its own host lock rather than by another vendor's queue:

    greenhouse       2 workers   4,294 boards   ~72 min   <- the bottleneck
    workable         2 workers   3,145 boards   ~52 min
    ashby            2 workers   2,651 boards   ~44 min
    smartrecruiters  1 worker      634 boards   ~22 min   (paginated)
    recruitee        1 worker      711 boards    ~6 min   (a host PER TOKEN)
    ------------------------------------------------------
                     8 == fetchers.http.MAX_CONCURRENCY

Lane throughput is capped at one request a second by the host lock however many
workers a lane has; the second worker on the big three exists only so that one
30-second timeout does not stall a 4,294-board queue. Nothing here raises
MAX_CONCURRENCY or PER_HOST_DELAY, and nothing here can: this sweep cannot hit
any vendor harder than the hourly crawl already does.

THE CANARY, WHICH IS A MEASUREMENT GUARD AND NOT JUST GOOD MANNERS
-------------------------------------------------------------------
A 429 storm exhausts http.py's three retries and fetchers/tier1.py returns [],
so BEING THROTTLED LOOKS EXACTLY LIKE A RUN OF DEAD BOARDS. Left alone that does
not just waste requests — it silently deflates the India count, which is the one
number this run exists to produce. Workable's own fetcher docstring says it
rate-limits aggressively, and its lane is 3,145 requests long.

So each lane remembers the last token that answered with jobs. After 25
consecutive empty results it re-fetches that known-live board before going on.
Still alive means the empties are real dead tokens and the lane carries on;
suddenly empty means we are being throttled — the lane pauses, slows itself
down, says so loudly, and RE-QUEUES the 25 boards it just mis-recorded. Re-probing
is safe because the write is an UPDATE keyed by id: a corrected verdict simply
overwrites the wrong one.

RESUMING
--------
Interrupt it and run it again. Verdicts are written every 25 results rather than
at the end, and the selection is `job_count IS NULL`, so a second run picks up
exactly what the first did not reach and re-fetches nothing it did. A board that
answered with nothing is written as job_count = 0, which is NOT NULL and is
therefore not re-probed — that distinction is the whole reason this script does
not reuse crawler.discover.record. Rows whose fetch THREW are also left alone;
--retry-errors is how you ask for those and only those.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crawler.verify_india import (  # noqa: E402
    EMPTY,
    ERROR,
    INDIA,
    NO_INDIA,
    SELECT_ERRORS_SQL,
    SELECT_UNPROBED_SQL,
    SR_MAX_PAGES,
    VERIFY_SQL,
    Board,
    Verdict,
    probe_board,
    verify_row,
)
from db.connection import get_crawler_conn  # noqa: E402
from fetchers.http import MAX_CONCURRENCY, close_client  # noqa: E402
from fetchers.tier1 import TIER1_ATS  # noqa: E402

SOURCE = "commoncrawl"

# Workers per lane. The total is asserted against fetchers.http.MAX_CONCURRENCY
# at start-up rather than trusted: if somebody lowers the semaphore, this
# allocation stops being starvation-free and the run should say so instead of
# quietly serialising. See the module docstring for why the total matters.
LANE_WORKERS: dict[str, int] = {
    "greenhouse": 2,
    "workable": 2,
    "ashby": 2,
    "smartrecruiters": 1,
    "recruitee": 1,
}

# Verdicts are flushed every this many results rather than at the end, so an
# interrupted sweep leaves behind what it had already learned. One executemany
# per batch keeps the write off the critical path: at ~2.3 boards a second the
# writer is idle most of the time either way.
BATCH = 25

# Consecutive empty/error results on one lane before the canary is consulted.
# Deliberately the same number as BATCH: it is the most boards a throttle can
# have mis-recorded before we notice, and it is what the lane re-queues.
CANARY_AFTER = 25

# How long a throttled lane sits out before trying again, and what its per-board
# extra delay becomes. The delay only ever grows.
THROTTLE_PAUSE = 60.0
THROTTLE_BACKOFF = 2.0

PROGRESS_EVERY = 100


@dataclass
class Lane:
    """One ATS: its queue, its workers, and what it has learned about the host."""

    ats: str
    boards: list[Board] = field(default_factory=list)
    workers: int = 1
    extra_delay: float = 0.0

    done: int = 0
    india: int = 0
    no_india: int = 0
    empty: int = 0
    error: int = 0
    started: float = 0.0

    # Throttle detection state.
    last_live_token: str | None = None
    consecutive_dead: int = 0
    recent_dead: list[Board] = field(default_factory=list)
    throttled: int = 0
    canary_checks: int = 0

    @property
    def total(self) -> int:
        return self.done + len(self.boards)

    @property
    def rate(self) -> float:
        """Boards a minute, measured rather than assumed."""
        elapsed = time.monotonic() - self.started
        return (self.done / elapsed * 60.0) if elapsed > 0 else 0.0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--limit", type=int, default=None, metavar="N",
        help="stop after N boards, spread evenly across the lanes (calibration)",
    )
    parser.add_argument(
        "--source", default=SOURCE, metavar="NAME",
        help=f"which discovered_companies.source to probe (default {SOURCE})",
    )
    parser.add_argument(
        "--retry-errors", action="store_true",
        help="re-probe only the rows whose previous fetch threw",
    )
    parser.add_argument(
        "--sr-max-pages", type=int, default=SR_MAX_PAGES, metavar="N",
        help=f"SmartRecruiters page cap, 100 jobs a page (default {SR_MAX_PAGES})",
    )
    parser.add_argument(
        "--lane-delay", action="append", default=[], metavar="ATS=SECONDS",
        help="extra pause per board on one lane, e.g. workable=2.0. Repeatable.",
    )
    parser.add_argument(
        "--report-only", action="store_true",
        help="print the report from what is already stored; fetch nothing",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="probe and report, but write nothing to discovered_companies",
    )
    return parser.parse_args(argv)


def parse_lane_delays(pairs: list[str]) -> dict[str, float]:
    """`['workable=2.0']` -> `{'workable': 2.0}`, refusing anything else."""
    delays: dict[str, float] = {}
    for pair in pairs:
        ats, _, value = pair.partition("=")
        ats = ats.strip()
        if ats not in LANE_WORKERS:
            raise SystemExit(
                f"--lane-delay: unknown ats {ats!r}; expected one of "
                f"{', '.join(sorted(LANE_WORKERS))}"
            )
        try:
            delays[ats] = max(0.0, float(value))
        except ValueError:
            raise SystemExit(f"--lane-delay: {value!r} is not a number") from None
    return delays


def interleave(boards: list[Board]) -> list[Board]:
    """Round-robin the boards across their ATSes, longest queue first.

    This is what makes `--limit 300` a calibration of every lane rather than 300
    Greenhouse boards, and it is why the limit is applied here rather than in
    SQL. For a full run the order within a lane does not matter — the lanes are
    independent queues — so this costs nothing.
    """
    by_ats: dict[str, list[Board]] = {}
    for board in boards:
        by_ats.setdefault(board.ats, []).append(board)
    queues = sorted(by_ats.values(), key=len, reverse=True)
    ordered: list[Board] = []
    for row in range(max((len(q) for q in queues), default=0)):
        for queue in queues:
            if row < len(queue):
                ordered.append(queue[row])
    return ordered


def build_lanes(boards: list[Board], delays: dict[str, float]) -> dict[str, Lane]:
    """Partition the work into one lane per ATS, with its worker allocation."""
    lanes: dict[str, Lane] = {}
    for board in boards:
        lane = lanes.get(board.ats)
        if lane is None:
            lane = lanes[board.ats] = Lane(
                ats=board.ats,
                workers=LANE_WORKERS.get(board.ats, 1),
                extra_delay=delays.get(board.ats, 0.0),
            )
        lane.boards.append(board)
    return lanes


# --- the sweep -------------------------------------------------------------


class Sweep:
    """The lanes, the shared result queue, and the one writer."""

    def __init__(self, lanes: dict[str, Lane], args: argparse.Namespace) -> None:
        self.lanes = lanes
        self.args = args
        self.results: asyncio.Queue[Verdict] = asyncio.Queue()
        self.pending: list[Verdict] = []
        self.written = 0
        self.recorded = 0
        self.unclear: Counter[str] = Counter()
        self.started = time.monotonic()

    # -- one board ----------------------------------------------------------

    async def run_board(self, lane: Lane, board: Board) -> None:
        verdict = await probe_board(board, sr_max_pages=self.args.sr_max_pages)
        self._note(lane, verdict)
        await self.results.put(verdict)
        if lane.extra_delay:
            await asyncio.sleep(lane.extra_delay)

    def _note(self, lane: Lane, verdict: Verdict) -> None:
        """Update the lane's tally and its throttle-detection state."""
        lane.done += 1
        if verdict.probe == INDIA:
            lane.india += 1
        elif verdict.probe == NO_INDIA:
            lane.no_india += 1
        elif verdict.probe == EMPTY:
            lane.empty += 1
        else:
            lane.error += 1

        if verdict.probe in (INDIA, NO_INDIA):
            # A board that answered is a board we can ask again later to find
            # out whether a silence is ours or theirs.
            lane.last_live_token = verdict.board.token
            lane.consecutive_dead = 0
            lane.recent_dead.clear()
        else:
            lane.consecutive_dead += 1
            lane.recent_dead.append(verdict.board)
            if len(lane.recent_dead) > CANARY_AFTER:
                lane.recent_dead.pop(0)

        if verdict.unclear_sample:
            self.unclear[verdict.unclear_sample] += 1

    # -- the canary ---------------------------------------------------------

    async def check_throttled(self, lane: Lane) -> None:
        """A run of empties: are these boards dead, or are we being blocked?

        The canary is never recorded. It is a question about the host, not a
        measurement of a board — that board's real verdict is already stored.
        """
        token = lane.last_live_token
        if not token:
            # Nothing on this lane has ever answered, so there is nothing to ask.
            # Say it once and stop counting, rather than warn every 25 boards.
            print(
                f"  ! {lane.ats}: {lane.consecutive_dead} dead in a row and no "
                f"known-live board yet — cannot tell throttling from dead tokens",
                flush=True,
            )
            lane.consecutive_dead = 0
            return

        lane.canary_checks += 1
        canary = await probe_board(
            Board(id=0, ats=lane.ats, token=token), sr_max_pages=self.args.sr_max_pages
        )
        lane.consecutive_dead = 0

        if canary.jobs:
            return  # the host is fine; those boards really are dead

        lane.throttled += 1
        requeue = list(lane.recent_dead)
        lane.recent_dead.clear()
        lane.extra_delay = max(lane.extra_delay * THROTTLE_BACKOFF, THROTTLE_BACKOFF)
        # Put them back at the FRONT: the sooner they are re-probed, the sooner
        # the wrong verdict already on disk is overwritten by the right one.
        lane.boards[:0] = requeue
        lane.done -= len(requeue)
        lane.empty = max(0, lane.empty - len(requeue))
        print(
            f"\n  !! {lane.ats}: THROTTLED — the known-live board {token!r} came "
            f"back empty too.\n"
            f"     Pausing {THROTTLE_PAUSE:.0f}s, per-board delay now "
            f"{lane.extra_delay:.1f}s, re-queueing {len(requeue)} board(s) whose "
            f"verdict this run cannot trust.\n",
            flush=True,
        )
        await asyncio.sleep(THROTTLE_PAUSE)

    # -- a lane worker ------------------------------------------------------

    async def worker(self, lane: Lane) -> None:
        while lane.boards:
            if lane.consecutive_dead >= CANARY_AFTER:
                await self.check_throttled(lane)
                continue
            board = lane.boards.pop(0)
            await self.run_board(lane, board)

    # -- the writer ---------------------------------------------------------

    async def flush(self, conn) -> None:
        if not self.pending or self.args.dry_run:
            self.pending.clear()
            return
        await conn.executemany(VERIFY_SQL, [verify_row(v) for v in self.pending])
        self.written += len(self.pending)
        self.pending.clear()

    async def writer(self, conn, total: int) -> None:
        """Record verdicts as they land, flushing every BATCH.

        Sequential because the crawler holds one asyncpg connection, and one
        executemany per 25 rather than one statement per board because the round
        trip is the only cost that scales with the number of boards here.
        """
        while self.recorded < total:
            verdict = await self.results.get()
            self.pending.append(verdict)
            self.recorded += 1
            if len(self.pending) >= BATCH:
                await self.flush(conn)
            if self.recorded % PROGRESS_EVERY == 0 or self.recorded == total:
                self.print_progress(total)
        await self.flush(conn)

    def print_progress(self, total: int) -> None:
        elapsed = time.monotonic() - self.started
        rate = (self.recorded / elapsed * 60.0) if elapsed else 0.0
        remaining = (total - self.recorded) / rate if rate else 0.0
        print(
            f"  … {self.recorded}/{total} probed  "
            f"({rate:.0f}/min, ~{remaining:.0f} min left)",
            flush=True,
        )
        for ats in sorted(self.lanes):
            lane = self.lanes[ats]
            print(
                f"      {ats:<16} {lane.done:>5}/{lane.total:<5} "
                f"{lane.rate:>5.1f}/min  india {lane.india:>4}  "
                f"no-india {lane.no_india:>4}  empty {lane.empty:>4}  "
                f"err {lane.error:>3}"
                + (f"  [+{lane.extra_delay:.1f}s]" if lane.extra_delay else ""),
                flush=True,
            )

    # -- run it -------------------------------------------------------------

    async def run(self, conn, total: int) -> None:
        tasks = [
            asyncio.create_task(self.worker(lane))
            for lane in self.lanes.values()
            for _ in range(lane.workers)
        ]
        writer = asyncio.create_task(self.writer(conn, total))
        try:
            await asyncio.gather(*tasks)
            await writer
        finally:
            # An interrupt must not leave probes running against live boards,
            # and whatever is already in hand must still be written.
            for task in tasks:
                task.cancel()
            writer.cancel()
            await self.flush(conn)


# --- the report ------------------------------------------------------------
#
# Computed from the TABLE rather than from what this process happens to hold in
# memory, so --report-only and the end of a run print the same thing by
# construction, and so a resumed sweep reports the whole population rather than
# the slice it personally probed.

STATE_SQL = """
SELECT coalesce(config->>'probe', '(never probed)') AS probe, count(*) AS n
  FROM discovered_companies
 WHERE source = $1
 GROUP BY 1
"""

BY_ATS_SQL = """
SELECT ats,
       count(*)                                                        AS total,
       count(*) FILTER (WHERE job_count IS NOT NULL)                   AS probed,
       count(*) FILTER (WHERE config->>'probe' IN ('india','no-india')) AS reachable,
       count(*) FILTER (WHERE config->>'probe' = 'india')              AS india_loose,
       count(*) FILTER (WHERE (config->>'india_named')::int > 0)       AS india_named,
       count(*) FILTER (WHERE config->>'probe' = 'empty')              AS dead,
       count(*) FILTER (WHERE config->>'probe' = 'error')              AS errored
  FROM discovered_companies
 WHERE source = $1
 GROUP BY ats
 ORDER BY total DESC, ats
"""

# The distribution, for whichever of the two numbers is asked for. The column
# name is interpolated from a fixed allow-list below, never from user input.
DIST_SQL = """
SELECT CASE WHEN n = 0        THEN '0'
            WHEN n = 1        THEN '1'
            WHEN n <= 5       THEN '2-5'
            WHEN n <= 20      THEN '6-20'
            ELSE                   '20+'
       END AS bucket,
       count(*) AS boards
  FROM (SELECT (config->>'{column}')::int AS n
          FROM discovered_companies
         WHERE source = $1 AND config->>'probe' IN ('india','no-india')) t
 GROUP BY 1
"""

RESIDUE_SQL = """
SELECT config->>'unclear_sample' AS location, count(*) AS boards
  FROM discovered_companies
 WHERE source = $1 AND config->>'unclear_sample' IS NOT NULL
 GROUP BY 1
 ORDER BY boards DESC, location
 LIMIT 40
"""

UNSUPPORTED_SQL = """
SELECT count(*) FROM discovered_companies
 WHERE source = $1 AND ats <> ALL($2::text[])
"""

BUCKETS = ("0", "1", "2-5", "6-20", "20+")


async def _dist(conn, source: str, column: str) -> dict[str, int]:
    assert column in ("india_jobs", "india_named")  # never user input
    rows = await conn.fetch(DIST_SQL.format(column=column), source)
    return {row["bucket"]: row["boards"] for row in rows}


async def print_report(conn, source: str) -> None:
    """Total probed, how many reachable, how many dead — and THE NUMBER."""
    states = {row["probe"]: row["n"] for row in await conn.fetch(STATE_SQL, source)}
    total = sum(states.values())
    probed = total - states.get("(never probed)", 0)
    reachable = states.get(INDIA, 0) + states.get(NO_INDIA, 0)

    print(f"\n{'=' * 72}\nINDIA VERIFY — source '{source}'\n{'=' * 72}")
    print(f"\n  harvested boards          {total:>7}")
    print(f"  probed                    {probed:>7}")
    print(f"  never probed (remaining)  {states.get('(never probed)', 0):>7}")
    print(f"\n  reachable (had postings)  {reachable:>7}")
    print(f"  dead / no postings        {states.get(EMPTY, 0):>7}   "
          f"(a 404, a delisted board and a live board with no openings are")
    print(f"  {'':>27}   indistinguishable here — tier1.fetch returns [] for all three)")
    print(f"  fetch errored             {states.get(ERROR, 0):>7}   "
          f"(re-probe these with --retry-errors)")

    india_loose = states.get(INDIA, 0)
    named = await conn.fetchval(
        "SELECT count(*) FROM discovered_companies "
        "WHERE source = $1 AND (config->>'india_named')::int > 0",
        source,
    )
    share = (100.0 * india_loose / reachable) if reachable else 0.0
    named_share = (100.0 * named / reachable) if reachable else 0.0

    print(f"\n{'-' * 72}\n  THE NUMBER — boards with at least one India job\n{'-' * 72}")
    print(f"\n  india-loose  {india_loose:>7}   {share:5.1f}% of reachable   "
          f"<- the stored bar: anything not non-india")
    print(f"  india-named  {named:>7}   {named_share:5.1f}% of reachable   "
          f"<- a CONFIDENT India statement")
    print(f"  unclear-only {india_loose - named:>7}   "
          f"{(100.0 * (india_loose - named) / india_loose) if india_loose else 0:5.1f}% "
          f"of india-loose  <- the inflation")
    print(
        "\n  The gap is boards whose entire India signal is `india-unclear`: an\n"
        "  unrecognised place name, a bare 'Remote', or an empty location. That\n"
        "  label means 'probably India' on a curated Indian board and inverts on a\n"
        "  random global one — see the residue table below for what is missing."
    )

    print(f"\n{'-' * 72}\n  BY ATS\n{'-' * 72}")
    header = (
        f"  {'ATS':<16} {'TOTAL':>7} {'PROBED':>7} {'REACH':>7} "
        f"{'LOOSE':>7} {'NAMED':>7} {'DEAD':>7} {'ERR':>5}"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))
    for row in await conn.fetch(BY_ATS_SQL, source):
        print(
            f"  {row['ats']:<16} {row['total']:>7} {row['probed']:>7} "
            f"{row['reachable']:>7} {row['india_loose']:>7} {row['india_named']:>7} "
            f"{row['dead']:>7} {row['errored']:>5}"
        )

    loose_dist = await _dist(conn, source, "india_jobs")
    named_dist = await _dist(conn, source, "india_named")
    print(f"\n{'-' * 72}\n  DISTRIBUTION — India jobs per reachable board\n{'-' * 72}")
    print(f"  {'BUCKET':<10} {'LOOSE':>10} {'NAMED':>10}")
    print("  " + "-" * 30)
    for bucket in BUCKETS:
        print(f"  {bucket:<10} {loose_dist.get(bucket, 0):>10} {named_dist.get(bucket, 0):>10}")

    unsupported = await conn.fetchval(UNSUPPORTED_SQL, source, list(TIER1_ATS))
    out_of_scope = await conn.fetchval(
        "SELECT count(*) FROM discovered_companies WHERE source <> $1 AND ats = 'unresolved'",
        source,
    )
    workday_queue = await conn.fetchval(
        "SELECT count(*) FROM discovered_companies WHERE config->>'note' = 'try Workday'"
    )
    print(f"\n{'-' * 72}\n  EXCLUDED\n{'-' * 72}")
    print(f"  unsupported board type / needed Workday: {unsupported}")
    print(
        f"    Structurally zero: fetchers/commoncrawl.py can only emit the five\n"
        f"    harvestable Tier-1 ATSes, and Workday is not among them — its tenant,\n"
        f"    pod and site are three unguessable strings, so no URL yields one.\n"
        f"  out of scope (other sources, unresolved names): {out_of_scope}, of which "
        f"{workday_queue} carry 'try Workday'.\n"
        f"    Those came from the seed list and GitHub mining, not this harvest, and\n"
        f"    are worked by hand with scripts/add_workday.py."
    )

    residue = await conn.fetch(RESIDUE_SQL, source)
    if residue:
        print(f"\n{'-' * 72}\n  INDIA-UNCLEAR RESIDUE — top {len(residue)} location strings\n{'-' * 72}")
        print(
            "  Each board's COMMONEST india-unclear location, counted by BOARDS (not\n"
            "  jobs). This is the evidence list for the markers the labeller is\n"
            "  missing — fixing them is a later task, not this run.\n"
        )
        print(f"  {'BOARDS':>7}  LOCATION")
        for row in residue:
            print(f"  {row['boards']:>7}  {row['location'][:80]}")

    await print_seal(conn)


async def print_seal(conn) -> None:
    """What this run was not allowed to touch, counted rather than promised."""
    companies = await conn.fetchval("SELECT count(*) FROM companies")
    jobs = await conn.fetchval("SELECT count(*) FROM jobs")
    promoted = await conn.fetchval("SELECT count(*) FROM discovered_companies WHERE promoted")
    print(f"\n{'-' * 72}\n  SEAL\n{'-' * 72}")
    print(f"  companies                {companies:>7}  — untouched by this script, by design")
    print(f"  jobs                     {jobs:>7}  — untouched by this script, by design")
    print(f"  discovered_companies promoted {promoted:>2}  — this run promotes nothing")


# --- main ------------------------------------------------------------------


async def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    delays = parse_lane_delays(args.lane_delay)

    allocated = sum(LANE_WORKERS.values())
    if not args.report_only and allocated != MAX_CONCURRENCY:
        print(
            f"WARNING: {allocated} lane worker(s) against a semaphore of "
            f"{MAX_CONCURRENCY}. The allocation is starvation-free only when the "
            f"two are equal; expect lanes to block each other.",
            file=sys.stderr,
        )

    conn = await get_crawler_conn()
    try:
        if args.report_only:
            await print_report(conn, args.source)
            return 0

        select = SELECT_ERRORS_SQL if args.retry_errors else SELECT_UNPROBED_SQL
        rows = await conn.fetch(select, args.source)
        boards = [
            Board(
                id=row["id"],
                ats=row["ats"],
                token=row["token"],
                config=json.loads(row["config"]) if isinstance(row["config"], str)
                else dict(row["config"] or {}),
            )
            for row in rows
            if row["ats"] in LANE_WORKERS
        ]
        skipped = len(rows) - len(boards)

        which = "errored" if args.retry_errors else "unprobed"
        print(f"{len(rows)} {which} row(s) for source '{args.source}'")
        if skipped:
            print(f"  {skipped} skipped: not on a lane (unresolved / unsupported ats)")

        boards = interleave(boards)
        if args.limit is not None:
            boards = boards[: args.limit]
            print(f"  --limit {args.limit}: probing {len(boards)}, spread across the lanes")

        if not boards:
            print("nothing to probe.")
            await print_report(conn, args.source)
            return 0

        lanes = build_lanes(boards, delays)
        print(f"\n{len(boards)} board(s) to probe across {len(lanes)} lane(s):")
        for ats in sorted(lanes):
            lane = lanes[ats]
            print(
                f"  {ats:<16} {len(lane.boards):>6} board(s), {lane.workers} worker(s)"
                + (f", +{lane.extra_delay:.1f}s each" if lane.extra_delay else "")
            )
        print(
            f"\nexpect roughly {max(len(l.boards) for l in lanes.values()) / 60.0:.0f}"
            f"-{max(len(l.boards) for l in lanes.values()) / 40.0:.0f} minutes "
            f"(the longest lane paces at ~1 board/second). A quiet terminal with a\n"
            f"moving progress line is a working run, not a hung one.\n"
        )

        sweep = Sweep(lanes, args)
        for lane in lanes.values():
            lane.started = time.monotonic()
        await sweep.run(conn, len(boards))

        elapsed = (time.monotonic() - sweep.started) / 60.0
        print(f"\nprobed {sweep.recorded} board(s) in {elapsed:.1f} min")
        if args.dry_run:
            print("--dry-run: nothing was written to discovered_companies.")
        else:
            print(f"wrote {sweep.written} verdict(s) to discovered_companies")
        throttled = sum(l.throttled for l in lanes.values())
        canaries = sum(l.canary_checks for l in lanes.values())
        print(
            f"canary checks: {canaries}, throttle events: {throttled}"
            + ("  <- re-run the affected lane with --lane-delay" if throttled else "")
        )

        await print_report(conn, args.source)
    finally:
        await close_client()
        await conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
