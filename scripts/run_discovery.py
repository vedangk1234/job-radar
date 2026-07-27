"""Sweep every candidate company name we can get hold of, and track the good ones.

    python scripts/run_discovery.py                       # seed list + GitHub
    python scripts/run_discovery.py --seed-file more.txt  # plus a list of my own
    python scripts/run_discovery.py --no-github           # seed list only
    python scripts/run_discovery.py --dry-run             # probe, report, write nothing

Candidates come from three places — scripts/discovery_seed.py, GitHub mining
(fetchers/discovery.py), and any --seed-file given — and all three are treated
identically from there on: crawler/discover.probe decides, crawler/discover.record
writes. See crawler/discover.py for what the two bars are and why the city test
happens before tracking rather than after.

The jobs table is not touched, here or anywhere downstream of here. A company
this script tracks has zero stored rows, so the next scripts/crawl.py detects a
first-ever crawl and seeds its whole back catalogue with notified = TRUE. That
is the same silent seed every other company got, and it is the reason a sweep
that tracks forty new boards does not produce forty boards' worth of email.

RESUMING
--------
Interrupt it and run it again: every candidate already in `companies` or already
in `discovered_companies` is skipped without a request, so the second run picks
up roughly where the first stopped. Each result is written as it lands rather
than at the end, which is what makes that true — a sweep that only wrote its
results on a clean exit would have nothing to resume from. Pass --recheck to
probe the logged near-misses again (a board with no Bangalore job in July may
have one in August).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crawler.discover import (  # noqa: E402
    ALREADY,
    FAILED,
    NO_CITY,
    TRACKED,
    UNRESOLVED,
    Candidate,
    Outcome,
    dedupe,
    load_known,
    probe,
    record,
)
from db.connection import get_crawler_conn  # noqa: E402
from fetchers.discovery import discover_candidates, github_token, summarise  # noqa: E402
from fetchers.http import close_client  # noqa: E402
from scripts.discovery_seed import SEED_CANDIDATES, is_enterprise, load_seed_file  # noqa: E402

# How many candidates are probed at once.
#
# This number does NOT decide how hard any vendor is hit — fetchers/http.py caps
# global in-flight requests at 8 and paces every host to one request a second,
# and nothing here can raise either. What it decides is whether those six host
# lanes are kept busy, and the first version of this script got that badly
# wrong. It probed in lockstep batches of ten, and a batch of ten candidates all
# walk the vendor list in the same order at the same time: ten requests pile onto
# the Greenhouse lane while Lever, Ashby, SmartRecruiters, Workable and Recruitee
# sit idle, then all ten move to Lever together. Six lanes, one of them working.
# Measured: ~1 request/second against a ceiling of six.
#
# The fix is to keep enough candidates in flight that they DRIFT OUT of step —
# one is on its fourth vendor while another starts its first — so every lane has
# somebody waiting on it. Twenty-four is comfortably past that point and still
# under thirty concurrent DNS resolutions of `{token}.recruitee.com`.
DEFAULT_CONCURRENCY = 24

# Results are written every this many candidates rather than at the end, so an
# interrupted sweep leaves behind what it had already learned. It doubles as the
# progress-report interval, which is why it is 10 and not 47. It is NOT a
# concurrency barrier: probes run continuously and results are recorded as they
# land, which is the other half of the lockstep fix above.
BATCH = 10


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--seed-file",
        action="append",
        default=[],
        metavar="PATH",
        help="extra candidate names, one per line (# comments allowed). Repeatable.",
    )
    parser.add_argument("--no-seed", action="store_true", help="skip the built-in seed list")
    parser.add_argument("--no-github", action="store_true", help="skip GitHub mining")
    parser.add_argument(
        "--github-limit", type=int, default=200, metavar="N",
        help="cap on candidates mined from GitHub (default 200)",
    )
    parser.add_argument(
        "--limit", type=int, default=None, metavar="N",
        help="stop after probing N candidates (after skips are applied)",
    )
    parser.add_argument(
        "--concurrency", type=int, default=DEFAULT_CONCURRENCY, metavar="N",
        help=f"candidates probed at once (default {DEFAULT_CONCURRENCY})",
    )
    parser.add_argument(
        "--max-variants", type=int, default=None, metavar="N",
        help="cap the token guesses per name (default: all of them)",
    )
    parser.add_argument(
        "--recheck", action="store_true",
        help="re-probe names already in discovered_companies",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="probe and report, but write nothing to either table",
    )
    return parser.parse_args(argv)


async def collect(args: argparse.Namespace) -> list[Candidate]:
    """Every candidate from every enabled source, deduped, in source order."""
    candidates: list[Candidate] = []

    if not args.no_seed:
        candidates.extend(
            Candidate(name=name, source="seed", enterprise=is_enterprise(name))
            for name in SEED_CANDIDATES
        )
        print(f"seed list: {len(candidates)} name(s)")

    for path in args.seed_file:
        names = load_seed_file(path)
        candidates.extend(
            Candidate(name=name, source="seed-file", enterprise=is_enterprise(name))
            for name in names
        )
        print(f"seed file {path}: {len(names)} name(s)")

    if not args.no_github:
        auth = "with GITHUB_TOKEN" if github_token() else "unauthenticated"
        print(f"mining github ({auth}, limit {args.github_limit}) …")
        mined = await discover_candidates(limit=args.github_limit)
        print("  " + summarise(mined))
        candidates.extend(
            Candidate(
                name=c.name, source=c.source, ats=c.ats, token=c.token, note=c.note
            )
            for c in mined
        )

    return dedupe(candidates)


def print_progress(done: int, total: int, tally: dict[str, int]) -> None:
    print(
        f"  … {done}/{total} probed — "
        f"tracked {tally[TRACKED]}, already {tally[ALREADY]}, "
        f"no-city {tally[NO_CITY]}, unresolved {tally[UNRESOLVED]}, failed {tally[FAILED]}",
        flush=True,
    )


def _truncate(text: str, width: int) -> str:
    text = text or ""
    return text if len(text) <= width else text[: width - 1] + "…"


def print_tracked_table(outcomes: list[Outcome]) -> None:
    """The full list of newly-tracked companies, with the evidence for each."""
    tracked = [o for o in outcomes if o.verdict == TRACKED]
    if not tracked:
        print("\nnewly tracked: none")
        return

    header = (
        f"{'NAME':<28} {'ATS':<16} {'TOKEN':<28} {'CITIES':<16} "
        f"{'IN-CITY':>7} {'BOARD':>6}"
    )
    print(f"\nnewly tracked: {len(tracked)} company/ies")
    print(header)
    print("-" * len(header))
    for o in sorted(tracked, key=lambda x: -x.in_city_jobs):
        print(
            f"{_truncate(o.candidate.name, 28):<28} "
            f"{o.ats or '-':<16} "
            f"{_truncate(o.token or '-', 28):<28} "
            f"{','.join(o.hits):<16} "
            f"{o.in_city_jobs:>7} {o.jobs_seen:>6}"
        )


def print_near_misses(outcomes: list[Outcome]) -> None:
    """Resolved boards that had no job in the three cities — logged, not tracked."""
    misses = [o for o in outcomes if o.verdict == NO_CITY]
    if not misses:
        return
    print(f"\nresolved but no blr/hyd/pune job ({len(misses)}, logged promoted=false):")
    for o in sorted(misses, key=lambda x: -x.jobs_seen)[:40]:
        spread = ", ".join(f"{k}={v}" for k, v in sorted(o.cities.items()))
        print(f"  {_truncate(o.candidate.name, 24):<24} {o.ats}:{_truncate(o.token or '', 24):<24} "
              f"{o.jobs_seen:>4} job(s)  [{spread}]")
    if len(misses) > 40:
        print(f"  … and {len(misses) - 40} more (see discovered_companies)")


def print_unresolved(outcomes: list[Outcome]) -> None:
    unresolved = [o for o in outcomes if o.verdict == UNRESOLVED]
    if not unresolved:
        return
    workday = [o for o in unresolved if o.note == "try Workday"]
    print(f"\nunresolved on every Tier-1 board: {len(unresolved)}")
    if workday:
        print(
            f"  {len(workday)} of them are enterprises/GCCs — logged with "
            f"note 'try Workday'. Tokens are not guessable there; add by URL "
            f"with scripts/add_workday.py."
        )
        print("  " + ", ".join(o.candidate.name for o in workday[:24]))
        if len(workday) > 24:
            print(f"  … and {len(workday) - 24} more")


async def print_totals(conn) -> None:
    companies = await conn.fetchval("SELECT count(*) FROM companies")
    tracked = await conn.fetchval("SELECT count(*) FROM companies WHERE tracked")
    discovered = await conn.fetchval("SELECT count(*) FROM discovered_companies")
    promoted = await conn.fetchval("SELECT count(*) FROM discovered_companies WHERE promoted")
    jobs = await conn.fetchval("SELECT count(*) FROM jobs")
    by_ats = await conn.fetch(
        "SELECT ats, count(*) AS n FROM companies WHERE tracked GROUP BY ats ORDER BY n DESC, ats"
    )
    spread = ", ".join(f"{row['ats']}={row['n']}" for row in by_ats)
    print(f"\ncompanies table: {companies} row(s), {tracked} tracked ({spread})")
    print(f"discovered_companies: {discovered} row(s), {promoted} promoted")
    print(f"jobs table: {jobs} row(s) — untouched by this script, by design")


async def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    candidates = await collect(args)
    print(f"\n{len(candidates)} distinct candidate(s) after de-duplication")

    conn = await get_crawler_conn()
    outcomes: list[Outcome] = []
    skipped: list[tuple[Candidate, str]] = []
    try:
        known = await load_known(conn)
        if args.recheck:
            known.logged_names.clear()
            known.logged_boards.clear()

        todo: list[Candidate] = []
        for candidate in candidates:
            why = known.seen(candidate)
            if why:
                skipped.append((candidate, why))
            else:
                todo.append(candidate)
        if args.limit is not None:
            todo = todo[: args.limit]

        print(f"{len(skipped)} skipped (already known), {len(todo)} to probe\n")

        semaphore = asyncio.Semaphore(max(1, args.concurrency))

        async def run_one(candidate: Candidate) -> Outcome:
            async with semaphore:
                return await probe(candidate, max_variants=args.max_variants)

        tally = {verdict: 0 for verdict in (TRACKED, ALREADY, NO_CITY, UNRESOLVED, FAILED)}

        # Every candidate is queued at once and the semaphore decides how many
        # run; results are recorded in COMPLETION order, as they land. Recording
        # is sequential because the crawler holds one asyncpg connection and it
        # cannot serve concurrent queries — but it happens in this loop rather
        # than at a batch boundary, so a slow candidate delays only its own row.
        pending = [asyncio.create_task(run_one(candidate)) for candidate in todo]
        try:
            for finished in asyncio.as_completed(pending):
                outcome = await finished
                if not args.dry_run:
                    outcome = await record(conn, outcome)
                tally[outcome.verdict] = tally.get(outcome.verdict, 0) + 1
                outcomes.append(outcome)
                if len(outcomes) % BATCH == 0 or len(outcomes) == len(todo):
                    print_progress(len(outcomes), len(todo), tally)
        finally:
            # An interrupt must not leave probes running against live boards.
            for task in pending:
                task.cancel()

        print(
            f"\ncandidates tried: {len(outcomes)}    "
            f"resolved: {tally[TRACKED] + tally[ALREADY] + tally[NO_CITY]}    "
            f"newly tracked: {tally[TRACKED]}    "
            f"resolved but no city match: {tally[NO_CITY]}    "
            f"unresolved: {tally[UNRESOLVED]}"
        )
        if tally[ALREADY]:
            print(
                f"({tally[ALREADY]} cleared both bars but companies already had the row — "
                f"left exactly as it was)"
            )
        if tally[FAILED]:
            print(f"({tally[FAILED]} probe(s) errored and were logged; nothing was tracked for them)")

        print_tracked_table(outcomes)
        print_near_misses(outcomes)
        print_unresolved(outcomes)

        if args.dry_run:
            print("\n--dry-run: nothing was written to companies or discovered_companies.")
        await print_totals(conn)
    finally:
        await close_client()
        await conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
