"""Harvest board tokens from the Common Crawl index into discovered_companies.

    python scripts/harvest_commoncrawl.py                      # newest crawl
    python scripts/harvest_commoncrawl.py --dry-run            # report, write nothing
    python scripts/harvest_commoncrawl.py --crawl-id CC-MAIN-2026-25

This is a THIRD candidate source, alongside the seed list and GitHub mining,
and it is the one that only ever produces a LIST. It queries the public Common
Crawl URL index for job-board URLs on the ATSes this project supports, pulls
`(ats, token)` out of each, drops everything already known, and writes the rest
to discovered_companies as the audit log.

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
It does not fetch a board, run the two bars, or promote anything. A row written
here has NULL job_count and NULL city_hits because nothing has looked at the
board yet — NULL is "never probed", which is a different and more honest thing
than 0. Verification happens later, by scripts/run_discovery.py or by hand.

It also does not filter by India, and cannot: the index carries URLs, not page
content, and every URL is on the ATS's own host. See fetchers/commoncrawl.py's
`india_hint` for the one weak, clearly-labelled guess that is stored alongside.

THE ONE STATEMENT
-----------------
`HARVEST_SQL` below is the only SQL this module runs that writes anything, and
it names exactly one table. tests/test_commoncrawl.py asserts that against the
statement TEXT rather than against this comment — the same way
tests/test_discovery.py proves the sweep never writes jobs. This script imports
no fetcher and no board client, so it cannot crawl a board even by mistake.

RESUMING
--------
Interrupt it and run it again. Rows are written per CDX page rather than at the
end, and every `(ats, token)` already in `companies` or `discovered_companies`
is skipped without a write — so a second run picks up what the first did not
reach, exactly like scripts/run_discovery.py.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crawler.discover import load_known, looks_like_services  # noqa: E402
from db.connection import get_crawler_conn  # noqa: E402
from fetchers.commoncrawl import (  # noqa: E402
    TARGETS,
    Target,
    fetch_page,
    india_hint,
    latest_crawl_id,
    num_pages,
    tokens_from_urls,
)
from fetchers.http import close_client  # noqa: E402

# The only writing statement in this module.
#
# ON CONFLICT DO NOTHING, never DO UPDATE — and deliberately not the DO UPDATE
# that crawler/discover.LOG_SQL uses. A harvested row knows strictly LESS than a
# probed one: no job count, no city hits, no promotion, no verdict. If a token
# is already in this table because the discovery sweep actually fetched its
# board, this statement must leave that row completely alone. DO NOTHING makes
# that structural rather than a promise the in-memory dedupe has to keep.
#
# job_count, city_hits and last_posting_at are omitted rather than zeroed. They
# stay NULL, which reads as "no one has looked at this board yet" — true, and
# distinguishable from a board that was probed and found empty.
HARVEST_SQL = """
INSERT INTO discovered_companies (name, ats, token, config,
                                  looks_like_services, source)
VALUES ($1, $2, $3, $4::jsonb, $5, 'commoncrawl')
ON CONFLICT (ats, token) DO NOTHING
"""

SOURCE = "commoncrawl"


def harvest_row(target: Target, token: str, crawl_id: str) -> tuple:
    """The discovered_companies parameter tuple for one harvested token.

    `name` is the token itself, which is all this source knows — the same
    choice fetchers/discovery.py makes for a board lifted out of a README.
    `looks_like_services` reuses the discovery sweep's own name heuristic so
    the flag means the same thing however a row arrived.
    """
    config: dict[str, object] = {
        "harvest": SOURCE,
        "crawl_id": crawl_id,
        "host": target.url.split("/")[0],
    }
    hint = india_hint(token)
    if hint:
        # A GUESS. It gates nothing — this row is stored either way — and the
        # reason is stored with it so a person can see which rule fired.
        config["india_hint"] = True
        config["india_hint_reason"] = hint

    return (token, target.ats, token, json.dumps(config), looks_like_services(token))


@dataclass
class Stat:
    """What one ATS produced. Greenhouse's two hosts aggregate into one row."""

    urls: int = 0
    tokens: int = 0
    known: int = 0
    new: int = 0
    pages: int = 0
    hinted: int = 0  # how many of `new` tripped the India guess
    notes: list[str] = field(default_factory=list)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--crawl-id",
        default=None,
        metavar="ID",
        help="monthly crawl to query, e.g. CC-MAIN-2026-30 (default: the newest)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="query and report, but write nothing to discovered_companies",
    )
    parser.add_argument(
        "--limit-pages",
        type=int,
        default=None,
        metavar="N",
        help="stop after N CDX pages per target (smoke-testing only)",
    )
    return parser.parse_args(argv)


def print_report(crawl_id: str, stats: dict[str, Stat], *, dry_run: bool) -> None:
    """The whole point of the first run: measurement, not estimation."""
    verb = "WOULD STORE" if dry_run else "STORED"
    header = (
        f"{'ATS':<18} {'PAGES':>5} {'URLS':>8} {'TOKENS':>7} "
        f"{'KNOWN':>6} {verb:>11} {'HINT?':>6}"
    )
    print(f"\nCommon Crawl harvest — {crawl_id}")
    print(header)
    print("-" * len(header))

    totals = Stat()
    for ats in sorted(stats):
        stat = stats[ats]
        print(
            f"{ats:<18} {stat.pages:>5} {stat.urls:>8} {stat.tokens:>7} "
            f"{stat.known:>6} {stat.new:>11} {stat.hinted:>6}"
        )
        totals.pages += stat.pages
        totals.urls += stat.urls
        totals.tokens += stat.tokens
        totals.known += stat.known
        totals.new += stat.new
        totals.hinted += stat.hinted
    print("-" * len(header))
    print(
        f"{'TOTAL':<18} {totals.pages:>5} {totals.urls:>8} {totals.tokens:>7} "
        f"{totals.known:>6} {totals.new:>11} {totals.hinted:>6}"
    )

    share = (100.0 * totals.hinted / totals.new) if totals.new else 0.0
    print(
        f"\n  HINT? is the India GUESS, made from the token text alone "
        f"({totals.hinted} of {totals.new}, {share:.2f}%). It gates nothing — "
        f"every row above is stored either way — and it misses most real India\n"
        f"  boards, because most do not put a country in their token. "
        f"Verification is the two-bar test, run later."
    )

    for ats in sorted(stats):
        for note in stats[ats].notes:
            print(f"\n  {ats}: {note}")

    if dry_run:
        print("\n--dry-run: nothing was written to discovered_companies.")


async def print_totals(conn) -> None:
    """What the table looks like now, and proof of what was not touched."""
    discovered = await conn.fetchval("SELECT count(*) FROM discovered_companies")
    from_cc = await conn.fetchval(
        "SELECT count(*) FROM discovered_companies WHERE source = $1", SOURCE
    )
    hinted = await conn.fetchval(
        "SELECT count(*) FROM discovered_companies "
        "WHERE source = $1 AND config->>'india_hint' = 'true'",
        SOURCE,
    )
    companies = await conn.fetchval("SELECT count(*) FROM companies")
    jobs = await conn.fetchval("SELECT count(*) FROM jobs")
    print(f"\ndiscovered_companies: {discovered} row(s), {from_cc} from {SOURCE}")
    print(
        f"  of those, {hinted} carry an india_hint — a GUESS from the token text "
        f"alone, which gates nothing and misses most real India boards. "
        f"Verification is the two-bar test, run later."
    )
    print(f"companies table: {companies} row(s) — untouched by this script, by design")
    print(f"jobs table: {jobs} row(s) — untouched by this script, by design")


async def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    crawl_id = args.crawl_id
    if not crawl_id:
        crawl_id = await latest_crawl_id()
        if not crawl_id:
            print(
                "could not read the newest crawl id from collinfo.json; "
                "pass --crawl-id explicitly",
                file=sys.stderr,
            )
            await close_client()
            return 1
        print(f"newest Common Crawl index: {crawl_id}")

    conn = await get_crawler_conn()
    stats: dict[str, Stat] = {}
    # Dedupe holds the whole run, not just one page: Greenhouse's two hosts
    # share an `ats`, so the same token legitimately arrives twice.
    seen: set[tuple[str, str]] = set()

    try:
        known = await load_known(conn)
        known_boards = known.company_boards | known.logged_boards
        print(f"{len(known_boards)} board(s) already known — those are skipped\n")

        for target in TARGETS:
            stat = stats.setdefault(target.ats, Stat())
            pages = await num_pages(crawl_id, target)
            if args.limit_pages is not None:
                pages = min(pages, args.limit_pages)
            stat.pages += pages

            for page in range(pages):
                urls = await fetch_page(crawl_id, target, page)
                stat.urls += len(urls)
                rows: list[tuple] = []
                for token in tokens_from_urls(target, urls):
                    key = (target.ats, token)
                    if key in seen:
                        continue
                    seen.add(key)
                    stat.tokens += 1
                    if key in known_boards:
                        stat.known += 1
                        continue
                    if india_hint(token):
                        stat.hinted += 1
                    rows.append(harvest_row(target, token, crawl_id))

                # Written per PAGE rather than at the end, so an interrupted
                # harvest leaves behind what it had already learned and the
                # next run resumes against it.
                if rows and not args.dry_run:
                    await conn.executemany(HARVEST_SQL, rows)
                stat.new += len(rows)

                print(
                    f"  … {target.label} p{page + 1}/{pages} — "
                    f"{len(urls)} url(s), {len(rows)} new",
                    flush=True,
                )

            if stat.tokens == 0 and target.expect_empty:
                # The tripwire fired as expected. Say so plainly: a silently
                # absent vendor is indistinguishable from a broken query.
                stat.notes.append(f"0 boards — {target.expect_empty}")

        print_report(crawl_id, stats, dry_run=args.dry_run)
        await print_totals(conn)
    finally:
        await close_client()
        await conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
