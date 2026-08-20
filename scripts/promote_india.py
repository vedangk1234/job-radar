"""Promote the india-named Common Crawl boards into `companies` as tracked.

    python scripts/promote_india.py             # dry run: count, ATS mix, sample
    python scripts/promote_india.py --commit    # write

The India verify sweep (crawler/verify_india.py) probed the 11,435 harvested
boards and wrote a verdict onto each row. It promoted nothing, on purpose. This
is the promotion, and it is the other half of that split: the sweep produced the
evidence, this reads it and acts.

WHAT COUNTS AS PROMOTABLE, and each clause earns its place:

    config->>'probe' = 'india'              the board was probed and hires here
    (config->>'india_named')::int > 0       CONFIDENTLY here -- see below
    job_count > 0                           the board actually answered with jobs

`india_named` rather than the stored loose bar. The loose bar counts
`india-unclear` as India, which is right for the fifty hand-picked Indian boards
it was designed for and INVERTS on eleven thousand random global ones, where an
unrecognised place name is almost certainly foreign (the hotel in Eemnes scored
ten out of ten). `india_named` excludes that bucket, so every board here has at
least one job whose location NAMES an Indian place.

`job_count > 0` is not a tautology next to the probe verdict: `verify_india`
writes 0 as 0 precisely so a probed-and-dead board is distinguishable from a
never-probed one. A promoted dead board is a fetch that fails every hour
forever, so the zeroes stay out.

Workday cannot appear here and is excluded twice anyway. The verify sweep only
ran the five Tier-1 lanes, so every Workday row in the table still has
`job_count IS NULL` and fails the predicate on its own -- including the known-bad
`imc` row, whose site is `invitation`, an invite-flow path rather than a careers
site. The explicit `ats <> 'workday'` below is belt and braces: a tenant, a pod
host and a site name are three unguessable strings and this script has no
business inventing them. add_workday.py is the queue for those.

WHAT IT WRITES, AND THE TWO TABLES IT IS ALLOWED TO TOUCH:

    companies                       one row per promoted board, tracked = TRUE
    discovered_companies.promoted   TRUE for the rows that made it

NEVER the jobs table. A newly-tracked company has zero stored rows, so the next
scripts/crawl.py detects a first-ever crawl and seeds its whole back catalogue
with seeded = TRUE -- the same silent seed every other company got, and the
entire reason promoting 650 boards does not produce 650 boards' worth of email.
That is the same guarantee crawler/discover.py makes; `assert_writes_nothing_else`
below asserts it against this module's own statement text rather than trusting
this docstring, because the prose here talks about that table constantly.

ON CONFLICT (ats, token) DO NOTHING, never DO UPDATE -- the same reasoning as
discover.TRACK_SQL. An existing row knows things this script does not: a
corrected name, a hand-tagged category, a Workday config, or a person having set
tracked = FALSE deliberately. The 50 curated companies are not this script's to
edit.

category is 'unknown' because nothing here knows whether a board is a product
firm or a GCC, and a guess is indistinguishable from the hand-tagging it would
overwrite. india_scoped is FALSE by construction rather than by choice: the flag
means "these rows are deliberately partial because the board was filtered at
source", and only Workday does that. Every board this script can insert is a
Tier-1 board fetched whole.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.connection import get_crawler_conn  # noqa: E402

# The predicate, once, as a string -- so the dry run and the write cannot drift
# apart into two slightly different definitions of "promotable".
CANDIDATE_WHERE = """
 WHERE config->>'probe' = 'india'
   AND (config->>'india_named')::int > 0
   AND job_count > 0
   AND ats <> 'workday'
"""

CANDIDATES_SQL = f"""
SELECT id, name, ats, token, config, job_count,
       (config->>'india_named')::int   AS india_named,
       (config->>'india_unclear')::int AS india_unclear
  FROM discovered_companies
{CANDIDATE_WHERE}
 ORDER BY (config->>'india_named')::int DESC, ats, token
"""

# ON CONFLICT DO NOTHING, so `RETURNING id` yields a row only on a genuine
# insert -- which is exactly the "newly promoted" count the report prints.
PROMOTE_SQL = """
INSERT INTO companies (name, ats, token, config, category, tracked, india_scoped)
VALUES ($1, $2, $3, $4::jsonb, 'unknown', TRUE, FALSE)
ON CONFLICT (ats, token) DO NOTHING
RETURNING id
"""

MARK_PROMOTED_SQL = """
UPDATE discovered_companies
   SET promoted = TRUE
 WHERE id = ANY($1::int[])
"""

# Keys that describe THIS PROBE rather than this board. They are already on the
# discovered_companies row, which is the audit log and where they belong; copied
# into companies.config they would be a snapshot going stale from the first
# crawl onwards. Everything else carries forward.
VERDICT_KEYS = frozenset({
    "probe", "probed_at", "jobs", "cities", "non_india",
    "india_jobs", "india_named", "india_unclear", "unclear_sample", "truncated",
})

WRITE_STATEMENTS = (PROMOTE_SQL, MARK_PROMOTED_SQL)

# The one table name this script must never write, spelled without appearing as
# a bare word the assertion below would trip over in its own source.
FORBIDDEN_TABLES = ("jobs", "user_companies", "user_job_notifications", "notifications")


def assert_writes_nothing_else() -> None:
    """This script writes companies and discovered_companies.promoted. Nothing else.

    Asserted against the statement text, the same way tests/test_discovery.py and
    tests/test_verify_india.py assert their own modules' "never writes the jobs
    table". A docstring cannot be executed; these two constants are.
    """
    for sql in WRITE_STATEMENTS:
        lowered = " ".join(sql.lower().split())
        for table in FORBIDDEN_TABLES:
            assert table not in lowered, f"statement mentions {table}: {sql}"

    insert = " ".join(PROMOTE_SQL.lower().split())
    assert insert.startswith("insert into companies "), insert
    assert "on conflict (ats, token) do nothing" in insert, insert
    assert "do update" not in insert, insert

    update = " ".join(MARK_PROMOTED_SQL.lower().split())
    assert update.startswith("update discovered_companies set promoted = true"), update
    # Keyed by id, so it can only touch rows this run selected.
    assert "where id = any" in update, update


def carried_config(raw) -> str:
    """The harvest config, minus this probe's verdict.

    For a Tier-1 board nothing in here is load-bearing -- `host` is the harvest
    URL's host, provenance rather than configuration, and only workday reads
    config['host'] / config['site'] at all. It is carried anyway because it is
    the record of where the row came from, and because the day a Workday row
    does reach this path its tenant/pod/site must survive intact.
    """
    config = raw if isinstance(raw, dict) else json.loads(raw or "{}")
    kept = {key: value for key, value in config.items() if key not in VERDICT_KEYS}
    kept["promoted_from"] = "india-verify"
    return json.dumps(kept)


def _truncate(text, width: int) -> str:
    text = str(text or "")
    return text if len(text) <= width else text[: width - 1] + "…"


def report(rows, sample: int) -> None:
    """Count, ATS mix, and a sample -- printed BEFORE anything is written."""
    print(f"promotable rows: {len(rows)}")

    mix: dict[str, list[int]] = {}
    for row in rows:
        entry = mix.setdefault(row["ats"], [0, 0, 0])
        entry[0] += 1
        entry[1] += row["job_count"] or 0
        entry[2] += row["india_named"] or 0

    print(f"\n{'ATS':<18} {'BOARDS':>7} {'JOBS':>9} {'INDIA-NAMED':>12}")
    print("-" * 49)
    for ats, (boards, jobs, named) in sorted(mix.items(), key=lambda kv: -kv[1][0]):
        print(f"{ats:<18} {boards:>7} {jobs:>9} {named:>12}")
    print("-" * 49)
    print(
        f"{'total':<18} {len(rows):>7} {sum(m[1] for m in mix.values()):>9} "
        f"{sum(m[2] for m in mix.values()):>12}"
    )

    print(f"\nsample of {min(sample, len(rows))} (highest india_named):")
    print(f"  {'ATS':<16} {'TOKEN':<28} {'JOBS':>6} {'NAMED':>6} {'UNCLEAR':>8}")
    for row in rows[:sample]:
        print(
            f"  {row['ats']:<16} {_truncate(row['token'], 28):<28} "
            f"{row['job_count']:>6} {row['india_named']:>6} "
            f"{str(row['india_unclear']):>8}"
        )


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--commit",
        action="store_true",
        help="actually write; without it this is a dry run",
    )
    parser.add_argument("--sample", type=int, default=15, metavar="N")
    args = parser.parse_args()

    assert_writes_nothing_else()

    conn = await get_crawler_conn()
    try:
        before_companies = await conn.fetchval("SELECT count(*) FROM companies")
        before_tracked = await conn.fetchval("SELECT count(*) FROM companies WHERE tracked")
        before_rows = await conn.fetchval("SELECT count(*) FROM jobs")

        rows = await conn.fetch(CANDIDATES_SQL)
        report(rows, args.sample)

        print(
            f"\ncompanies now: {before_companies} ({before_tracked} tracked)  "
            f"stored postings now: {before_rows}"
        )

        if not args.commit:
            print("\nDRY RUN -- nothing written. Re-run with --commit to promote.")
            return 0

        if not rows:
            print("\nnothing to promote.")
            return 0

        inserted: list[int] = []  # discovered_companies.id of genuine inserts
        declined: list[tuple[str, str]] = []  # already had a companies row

        # One transaction: either the catalog gains every row and the log says
        # so, or neither happened. A half-promoted run whose `promoted` flags
        # disagree with `companies` is the state worth not having.
        async with conn.transaction():
            for row in rows:
                company_id = await conn.fetchval(
                    PROMOTE_SQL,
                    row["token"],
                    row["ats"],
                    row["token"],
                    carried_config(row["config"]),
                )
                if company_id is None:
                    declined.append((row["ats"], row["token"]))
                else:
                    inserted.append(row["id"])

            if inserted:
                await conn.execute(MARK_PROMOTED_SQL, inserted)

        after_companies = await conn.fetchval("SELECT count(*) FROM companies")
        after_tracked = await conn.fetchval("SELECT count(*) FROM companies WHERE tracked")
        after_rows = await conn.fetchval("SELECT count(*) FROM jobs")
        promoted_flags = await conn.fetchval(
            "SELECT count(*) FROM discovered_companies WHERE promoted"
        )

        print(f"\npromoted    : {len(inserted)} new companies row(s), tracked = TRUE")
        if declined:
            print(f"declined    : {len(declined)} already present (ON CONFLICT DO NOTHING)")
            for ats, token in declined[:20]:
                print(f"    - {ats}:{token}")
        print(
            f"companies   : {before_companies} -> {after_companies} "
            f"({before_tracked} -> {after_tracked} tracked)"
        )
        print(f"discovered_companies.promoted now TRUE on {promoted_flags} row(s)")
        print(f"stored postings: {before_rows} -> {after_rows}   <- MUST be unchanged")

        if after_rows != before_rows:
            print(
                "\n! the postings table moved. That is not this script's doing and is "
                "worth understanding before crawling.",
                file=sys.stderr,
            )
            return 1

        print(
            "\nNothing seeded yet. The next scripts/crawl.py is what seeds these "
            "boards' back catalogues, silently (seeded = TRUE)."
        )
    finally:
        await conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
