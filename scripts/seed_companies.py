"""Seed the companies table from a hand-picked candidate list.

For each candidate: guess its ATS and board token, fetch the whole board once,
archive the raw response as a fixture, and upsert the COMPANY row.

Deliberately does not touch the jobs table. This step establishes who we watch;
recording what they posted is a later step with its own first_seen semantics,
and writing rows here would poison that.

    python scripts/seed_companies.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.connection import get_crawler_conn  # noqa: E402
from fetchers.fixtures import FIXTURES_DIR, save_fixture  # noqa: E402
from fetchers.http import close_client  # noqa: E402
from fetchers.normalize import city_label  # noqa: E402
from fetchers.resolve import resolve  # noqa: E402
from fetchers.tier1 import fetch  # noqa: E402

CANDIDATES = [
    "razorpay", "postman", "groww", "cred", "zeta", "meesho",
    "phonepe", "browserstack", "hasura", "chargebee", "zerodha", "zepto",
    "atlan", "juspay", "setu", "innovaccer", "navi", "khatabook",
]

UPSERT = """
INSERT INTO companies (name, ats, token, config, category, tracked, india_scoped)
VALUES ($1, $2, $3, '{}'::jsonb, 'unknown', TRUE, FALSE)
ON CONFLICT (ats, token) DO UPDATE
   SET name     = EXCLUDED.name,
       tracked  = TRUE,
       -- category is hand-tagged once; never stomp an existing tag.
       category = COALESCE(companies.category, 'unknown')
RETURNING id
"""


class Result:
    """One candidate's outcome, in the shape the summary table wants."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.ats: str | None = None
        self.token: str | None = None
        self.job_count = 0
        self.fixture: str | None = None
        self.cities: dict[str, int] = {}

    @property
    def resolved(self) -> bool:
        return self.ats is not None


async def probe(name: str) -> Result:
    """Resolve one candidate and fetch its board. Never raises."""
    result = Result(name)
    try:
        found = await resolve(name)
    except Exception as exc:  # a single bad board must not sink the run
        print(f"  ! {name}: resolve failed ({type(exc).__name__}: {exc})", file=sys.stderr)
        return result
    if found is None:
        return result

    result.ats, result.token, _ = found

    # Candidates run concurrently, so identify this board's fixture by its own
    # {ats}_{token}_ prefix rather than by "whatever appeared while I waited".
    prefix = f"{result.ats}_{result.token}_"
    before = {p.name for p in FIXTURES_DIR.glob(f"{prefix}*.json")} if FIXTURES_DIR.exists() else set()
    try:
        jobs = await fetch(result.ats, result.token)
    except Exception as exc:
        print(f"  ! {name}: fetch failed ({type(exc).__name__}: {exc})", file=sys.stderr)
        jobs = []

    result.job_count = len(jobs)
    for job in jobs:
        label = city_label(job["location"])
        result.cities[label] = result.cities.get(label, 0) + 1

    after = {p.name for p in FIXTURES_DIR.glob(f"{prefix}*.json")} if FIXTURES_DIR.exists() else set()
    new_files = sorted(after - before)
    if new_files:
        result.fixture = new_files[-1]
    else:
        # Resolved but the re-fetch came back empty, so no fixture was written
        # by the fetcher. Archive the outcome anyway — every board we touch
        # leaves a trace.
        result.fixture = save_fixture(result.ats, result.token, {"jobs": [], "note": "empty refetch"}).name
    return result


def _truncate(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


async def main() -> int:
    print(f"resolving {len(CANDIDATES)} candidates across greenhouse -> lever -> ashby …\n")
    try:
        results = await asyncio.gather(*(probe(name) for name in CANDIDATES))
    finally:
        await close_client()

    conn = await get_crawler_conn()
    try:
        for result in results:
            if result.resolved:
                await conn.execute(UPSERT, result.name, result.ats, result.token)
        total_rows = await conn.fetchval("SELECT count(*) FROM companies")
    finally:
        await conn.close()

    header = (
        f"{'COMPANY':<14} {'RESOLVED':<9} {'ATS':<16} {'TOKEN':<32} {'#JOBS':>6}  FIXTURE"
    )
    print(header)
    print("-" * len(header))
    for result in results:
        print(
            f"{result.name:<14} "
            f"{'yes' if result.resolved else 'no':<9} "
            f"{result.ats or '-':<16} "
            f"{_truncate(result.token or '-', 32):<32} "
            f"{result.job_count if result.resolved else '-':>6}  "
            f"{result.fixture or '-'}"
        )

    resolved = [r for r in results if r.resolved]
    jobs_seen = sum(r.job_count for r in resolved)
    cities: dict[str, int] = {}
    for result in resolved:
        for label, count in result.cities.items():
            cities[label] = cities.get(label, 0) + count

    print(f"\ntotal resolved: {len(resolved)}/{len(CANDIDATES)}")
    print(f"jobs seen (crawled, not stored): {jobs_seen}")
    if cities:
        print("city labels across all boards: " + ", ".join(f"{k}={v}" for k, v in sorted(cities.items())))
    print(f"companies table now holds {total_rows} row(s); jobs table untouched by design.")

    unresolved = [r.name for r in results if not r.resolved]
    if unresolved:
        print("\nunresolved (token is not a mechanical variant of the name): " + ", ".join(unresolved))
    return 0 if len(resolved) >= 6 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
