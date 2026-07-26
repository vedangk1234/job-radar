"""Verify a Workday tenant against the live site, then upsert it into companies.

    python scripts/add_workday.py                      # the verified defaults
    python scripts/add_workday.py "<careers url>" ...  # ad-hoc tenants

For each careers URL: parse it into a config, fetch the India-scoped board once,
archive the raw responses as fixtures, print a sample of normalised rows, and
upsert the COMPANY row. Idempotent on (ats, token).

Deliberately does not touch the jobs table — same rule as seed_companies.py.
The first `python scripts/crawl.py` after this is what seeds the jobs, silently.

A tenant whose URL parses ambiguously, or that raises, is REPORTED AND SKIPPED
rather than stored: a companies row with a wrong host is a crawl that fails
every 30 minutes forever.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.connection import get_crawler_conn  # noqa: E402
from fetchers.http import close_client  # noqa: E402
from fetchers.normalize import city_label  # noqa: E402
from fetchers.resolve import parse_workday_url  # noqa: E402
from fetchers.workday import WorkdayError, fetch_workday_detailed  # noqa: E402

# Confirmed by opening each careers site and reading the /wday/cxs/ call it
# makes. Walmart's tenant is deliberately absent: walmart.wd5 answers the jobs
# endpoint with an HTML page rather than JSON, so its site name is something
# other than the obvious guess and it is not worth guessing at.
CANDIDATES = [
    ("NVIDIA", "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite"),
    ("Adobe", "https://adobe.wd5.myworkdayjobs.com/en-US/external_experienced"),
    ("Salesforce", "https://salesforce.wd12.myworkdayjobs.com/en-US/External_Career_Site"),
]

UPSERT = """
INSERT INTO companies (name, ats, token, config, category, tracked, india_scoped)
VALUES ($1, 'workday', $2, $3::jsonb, 'gcc', TRUE, $4)
ON CONFLICT (ats, token) DO UPDATE
   SET name         = EXCLUDED.name,
       config       = EXCLUDED.config,
       tracked      = TRUE,
       india_scoped = EXCLUDED.india_scoped,
       category     = COALESCE(companies.category, 'gcc')
RETURNING id
"""


def _truncate(text: str, width: int) -> str:
    text = text or ""
    return text if len(text) <= width else text[: width - 1] + "…"


async def verify(name: str, careers_url: str) -> dict | None:
    """Parse, fetch, print a sample. Returns the row to upsert, or None."""
    print(f"\n=== {name}  {careers_url}")
    config = parse_workday_url(careers_url)
    print(f"  parsed: {json.dumps(config)}")
    if config.get("ambiguous"):
        print(f"  ! ambiguous {config['ambiguous']} — not storing, confirm by hand", file=sys.stderr)
        return None

    try:
        fetched = await fetch_workday_detailed(config["tenant"], config)
    except WorkdayError as exc:
        print(f"  ! fetch failed, not storing — {exc}", file=sys.stderr)
        return None

    rows = fetched.rows
    cities: dict[str, int] = {}
    for row in rows:
        label = city_label(row["location"])
        cities[label] = cities.get(label, 0) + 1

    print(f"  fetched {len(rows)} row(s); board total {fetched.board_total}; "
          f"india_scoped={fetched.scoped} via {fetched.facet}")
    print(f"  city labels: " + ", ".join(f"{k}={v}" for k, v in sorted(cities.items())))
    print(f"  {'TITLE':<42} {'CITY':<14} {'LOCATION':<26} URL")
    for row in rows[:8]:
        print(
            f"  {_truncate(row['title'], 42):<42} "
            f"{city_label(row['location']):<14} "
            f"{_truncate(row['location'], 26):<26} "
            f"{row['url']}"
        )

    stored = {k: config[k] for k in ("host", "site", "lang", "careers_url") if config.get(k)}
    return {
        "name": name,
        "token": config["tenant"],
        "config": stored,
        "india_scoped": fetched.scoped,
        "count": len(rows),
    }


async def main() -> int:
    args = sys.argv[1:]
    candidates = [(u.split("//")[-1].split(".")[0], u) for u in args] if args else CANDIDATES

    try:
        results = [await verify(name, url) for name, url in candidates]
    finally:
        await close_client()

    good = [r for r in results if r]
    if not good:
        print("\nnothing verified; companies table untouched.", file=sys.stderr)
        return 1

    conn = await get_crawler_conn()
    try:
        for row in good:
            company_id = await conn.fetchval(
                UPSERT, row["name"], row["token"], json.dumps(row["config"]), row["india_scoped"]
            )
            print(f"upserted companies id={company_id}  {row['name']}  workday:{row['token']}  "
                  f"india_scoped={row['india_scoped']}  ({row['count']} jobs seen, none stored)")
        total = await conn.fetchval("SELECT count(*) FROM companies")
        jobs = await conn.fetchval("SELECT count(*) FROM jobs")
    finally:
        await conn.close()

    print(f"\ncompanies table now holds {total} row(s); jobs table still at {jobs} (untouched by design).")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
