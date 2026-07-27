"""Crawl every tracked company once, reconcile the jobs table, read what fits.

    python scripts/crawl.py

Stores and diffs, then enriches. Sends nothing. Brand-new jobs are left with
notified=FALSE, which is the queue Step 4 will read; on a company's first-ever
crawl they are stored notified=TRUE instead, so a hundred-job back catalogue is
recorded silently rather than emailed.

The enrichment pass runs once, AFTER every company has been reconciled, rather
than once per company. Two reasons, and both are about being polite: a job that
appeared and vanished within the same run is never fetched, and the board-level
description caches in notify/description.py are shared across the whole pass
instead of being warmed and thrown away twelve times. See crawler/enrich.py for
what the pass is for — in one line, it is why a 4-6-years posting stops reaching
the dashboard within one crawl of being discovered rather than never.

Seeding is auto-detected per company from its row count, not from a flag, so
there is no way to forget to turn it off — and a company added to the list next
month seeds itself on its own schedule while everyone else keeps alerting.

Companies run one at a time. The politeness layer from Step 2 already paces
per host, and the crawler holds a single asyncpg connection, which cannot serve
concurrent queries; sequential keeps both facts uncomplicated.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crawler.enrich import EnrichmentPass, enrich_matches  # noqa: E402
from crawler.loop import Company, CrawlResult, crawl  # noqa: E402
from crawler.store import count_jobs  # noqa: E402
from db.connection import get_crawler_conn  # noqa: E402
from fetchers.http import close_client  # noqa: E402

COMPANIES_SQL = """
SELECT id, name, ats, token, config
  FROM companies
 WHERE tracked = TRUE
 ORDER BY id
"""


def _truncate(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def print_table(results: list[CrawlResult]) -> None:
    header = (
        f"{'COMPANY':<14} {'FETCHED':>7} {'NEW':>5} {'REAPP':>6} {'MISSING':>7} "
        f"{'CLOSED':>6}  {'SEEDING':<8} GUARD"
    )
    print(header)
    print("-" * len(header))
    for r in results:
        print(
            f"{_truncate(r.company, 14):<14} "
            f"{r.fetched:>7} {r.truly_new:>5} {r.reappeared:>6} {r.missing:>7} "
            f"{r.closed:>6}  "
            f"{'yes' if r.seeding else 'no':<8} "
            f"{r.guard or '-'}"
        )


def print_enrichment(pass_: EnrichmentPass) -> None:
    """What reading descriptions cost, and what it changed.

    `hidden` is the line worth reading: those jobs passed every title test, so
    until their descriptions were read they were matches on Home and would have
    been emailed. Each one is now excluded for a reason that is written down.
    """
    print(
        f"\nenrichment: {pass_.title_stage} title-stage match(es) needed a description "
        f"(of {pass_.considered} unchecked open row(s))"
    )
    print(
        f"  desc_checked flipped this run: {pass_.checked}  "
        f"({pass_.read} read, {pass_.failed} unavailable — kept, retried in 24h)"
    )

    if not pass_.hidden:
        print("  newly hidden by their description: 0")
        return

    print(f"  newly hidden by their description: {len(pass_.hidden)}")
    for job in pass_.hidden:
        years = job["min_experience"]
        why = "PM role" if job["is_pm_role"] else f"min {years} yrs"
        print(f"    - {job['company']} — {job['title']}  [{why}]")


async def print_totals(conn) -> None:
    """Whole-table state after the run: size, queue depth, city spread."""
    total = await conn.fetchval("SELECT count(*) FROM jobs")
    notified = await conn.fetchval("SELECT count(*) FROM jobs WHERE notified")
    unnotified = await conn.fetchval("SELECT count(*) FROM jobs WHERE NOT notified")
    open_rows = await conn.fetchval("SELECT count(*) FROM jobs WHERE status = 'open'")
    closed_rows = await conn.fetchval("SELECT count(*) FROM jobs WHERE status = 'closed'")
    cities = await conn.fetch(
        "SELECT city, count(*) AS n FROM jobs GROUP BY city ORDER BY n DESC, city"
    )

    hidden_years = await conn.fetchval(
        "SELECT count(*) FROM jobs WHERE status = 'open' AND min_experience >= 3"
    )
    hidden_pm = await conn.fetchval(
        "SELECT count(*) FROM jobs WHERE status = 'open' AND is_pm_role"
    )
    unchecked = await conn.fetchval(
        "SELECT count(*) FROM jobs WHERE status = 'open' AND NOT desc_checked"
    )

    print(f"\njobs table: {total} row(s)  ({open_rows} open, {closed_rows} closed)")
    print(f"notified: TRUE={notified}  FALSE={unnotified}   <- FALSE is Step 4's queue")
    print("city labels: " + ", ".join(f"{r['city']}={r['n']}" for r in cities))
    print(
        f"description facts: {hidden_years} open row(s) demand 3+ years, "
        f"{hidden_pm} are PM roles — both hidden everywhere"
    )
    print(
        f"  still unchecked: {unchecked} open row(s) — the board minus the title-stage "
        f"matches, which the pass above just read"
    )


async def main() -> int:
    conn = await get_crawler_conn()
    try:
        companies = [Company.from_row(row) for row in await conn.fetch(COMPANIES_SQL)]
        print(f"crawling {len(companies)} tracked companies …\n")

        results: list[CrawlResult] = []
        for company in companies:
            # Zero stored rows, open or closed = never crawled = seed silently.
            seeding = await count_jobs(conn, company.id) == 0
            results.append(await crawl(conn, company, seeding=seeding))

        print_table(results)

        seeded = [r for r in results if r.seeding and not r.guard_tripped]
        would_alert = sum(r.truly_new for r in results if not r.seeding and not r.guard_tripped)
        tripped = [r for r in results if r.guard_tripped]

        print(f"\nseeded this run: {len(seeded)} company/ies (their jobs stored notified=TRUE)")
        print(f"would-be alerts queued this run: {would_alert} job(s)")
        if tripped:
            print("guards tripped (no writes for these): " + ", ".join(f"{r.company} [{r.guard}]" for r in tripped))

        # Every company is reconciled by now, so this sees the table's final
        # state for the run — including jobs that appeared in this very crawl.
        print_enrichment(await enrich_matches(conn))

        await print_totals(conn)
    finally:
        await close_client()
        await conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
