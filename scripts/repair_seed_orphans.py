"""Flip back-catalogue rows that missed the silent seed to seeded = TRUE.

    python scripts/repair_seed_orphans.py             # dry run: the list
    python scripts/repair_seed_orphans.py --commit    # write

WHY THERE ARE ANY. `crawler/loop.crawl` stores a board and then marks the new
rows, and those are two statements rather than one:

    await upsert_seen(conn, company.id, fetched)      # seeded takes its FALSE default
    await mark_new(conn, d.truly_new, seeded=seeding) # ... and is corrected here

`jobs.seeded` is NOT NULL DEFAULT FALSE and is absent from UPSERT_SEEN's insert
list on purpose -- mark_new is the only thing that knows whether this is a
company's first-ever crawl. The gap between the two is therefore a window in
which a company's entire back catalogue is sitting at seeded = FALSE, which is
to say EMAILABLE, and anything that interrupts the process inside that window
leaves it that way.

Two things open that window. A crawl killed mid-company -- the 20-minute
GitHub Actions timeout against a 700-company catalog is the live example. And
TWO CRAWLERS RACING: the second one finds rows already stored, so `count_jobs`
is non-zero, so it crawls with seeding = FALSE, and every posting its fetch
returns that the first one's did not gets marked new and emailable. That is what
produced the rows this script exists to fix, when a late-running hourly cron
overlapped the manual promotion crawl.

WHY seeded = TRUE IS THE CORRECT VALUE HERE, and not merely the convenient one.
`seeded` means "this row is back catalogue" -- a property of the crawl, not of
any person. A company promoted by scripts/promote_india.py had zero stored rows
by construction, so until its FIRST crawl completes, every row on it is back
catalogue by definition. A posting that showed up on page 3 for one crawler and
page 4 for another is not news; it is the same back catalogue, observed twice.

THE SCOPE IS DELIBERATELY NARROW, and each clause is load-bearing:

    companies.config->>'promoted_from' = 'india-verify'
        only boards this promotion added. The 50 curated companies have real
        crawl history and their unseeded rows are genuine alerts.

    NOT seeded
        the rows in question.

    first_seen_at < the cutoff
        the seeding window. Without it this script would silence real news the
        moment it is run a second time, which is the failure that matters: a
        job that appears on a promoted board NEXT week is news and must stay
        seeded = FALSE. The cutoff defaults to the newest first_seen_at among
        rows that DID get seeded on these companies, plus a small margin --
        i.e. the end of the seeding event, derived rather than typed.

It writes ONE column on ONE table. Never `companies`, never
`user_job_notifications`, never `notifications` -- a row that was genuinely
emailed to somebody has a receipt there, and this script has no business
editing history. Asserted below against the statement text.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.connection import get_crawler_conn  # noqa: E402

PROMOTED = "c.config->>'promoted_from' = 'india-verify'"

# The end of the seeding event, derived from the data rather than typed: the
# newest first_seen_at among rows that DID get seeded on these companies.
CUTOFF_SQL = f"""
SELECT max(j.first_seen_at)
  FROM jobs j JOIN companies c ON c.id = j.company_id
 WHERE {PROMOTED} AND j.seeded
"""

ORPHANS_SQL = f"""
SELECT c.id AS company_id, c.ats, c.token, j.canonical_id, j.title, j.city,
       j.first_seen_at, j.last_seen_at
  FROM jobs j JOIN companies c ON c.id = j.company_id
 WHERE {PROMOTED}
   AND NOT j.seeded
   AND j.first_seen_at < $1
 ORDER BY c.id, j.canonical_id
"""

# Keyed by the exact canonical_ids the dry run listed, so the write can only
# touch rows a person has seen printed.
REPAIR_SQL = """
UPDATE jobs
   SET seeded = TRUE
 WHERE canonical_id = ANY($1::text[])
   AND NOT seeded
RETURNING canonical_id
"""


def assert_writes_one_column() -> None:
    """One column, one table. Asserted against the statement rather than promised."""
    sql = " ".join(REPAIR_SQL.lower().split())
    assert sql.startswith("update jobs set seeded = true"), sql
    assert "where canonical_id = any" in sql, sql
    for forbidden in ("companies", "user_job_notifications", "notifications",
                      "discovered_companies", "delete", "insert"):
        assert forbidden not in sql, f"statement mentions {forbidden}: {sql}"


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--commit", action="store_true",
                        help="actually write; without it this is a dry run")
    parser.add_argument("--margin-minutes", type=int, default=5, metavar="N",
                        help="grace added to the derived cutoff (default 5)")
    args = parser.parse_args()

    assert_writes_one_column()

    conn = await get_crawler_conn()
    try:
        cutoff = await conn.fetchval(CUTOFF_SQL)
        if cutoff is None:
            print("no seeded rows on promoted companies — nothing to derive a cutoff from.")
            return 1
        cutoff = cutoff + timedelta(minutes=args.margin_minutes)
        print(f"seeding-window cutoff: {cutoff.isoformat()}  "
              f"(newest seeded first_seen_at + {args.margin_minutes}m)")

        orphans = await conn.fetch(ORPHANS_SQL, cutoff)
        later = await conn.fetchval(
            f"""SELECT count(*) FROM jobs j JOIN companies c ON c.id=j.company_id
                 WHERE {PROMOTED} AND NOT j.seeded AND j.first_seen_at >= $1""", cutoff)

        print(f"\nback-catalogue rows still at seeded = FALSE: {len(orphans)}")
        print(f"unseeded rows AFTER the window (left alone, genuine news): {later}")

        if orphans:
            print(f"\n  {'COMPANY':<40} {'CITY':<14} TITLE")
            for row in orphans:
                who = f"{row['ats']}:{row['token']}"
                print(f"  {who[:40]:<40} {row['city']:<14} {str(row['title'])[:52]}")

        if not args.commit:
            print("\nDRY RUN — nothing written. Re-run with --commit to repair.")
            return 0
        if not orphans:
            print("\nnothing to repair.")
            return 0

        ids = [row["canonical_id"] for row in orphans]
        before = await conn.fetchval("SELECT count(*) FROM jobs WHERE seeded")
        async with conn.transaction():
            repaired = await conn.fetch(REPAIR_SQL, ids)
        after = await conn.fetchval("SELECT count(*) FROM jobs WHERE seeded")
        remaining = await conn.fetchval(
            f"""SELECT count(*) FROM jobs j JOIN companies c ON c.id=j.company_id
                 WHERE {PROMOTED} AND NOT j.seeded AND j.first_seen_at < $1""", cutoff)

        print(f"\nrepaired : {len(repaired)} row(s) flipped to seeded = TRUE")
        print(f"seeded   : {before} -> {after}")
        print(f"remaining back-catalogue rows unseeded: {remaining}   <- MUST be 0")
        return 0 if remaining == 0 else 1
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
