"""Recompute jobs.city for every stored row with the current labeller.

    python scripts/relabel_cities.py            # apply
    python scripts/relabel_cities.py --dry-run  # report only, write nothing

WHY THIS IS NEEDED AT ALL
-------------------------
`city` is computed once, at crawl time, by fetchers.normalize.city_label and
written to the row (see crawler/store.upsert_seen). That is deliberate — a label
is cheap to recompute and expensive to backfill — but it means a change to the
labeller does not reach the rows already stored. Every job crawled before the
change keeps the label the old rules gave it, forever, because upsert_seen only
rewrites `city` when the board serves that job again.

So a labeller change needs this pass to land. Without it, adding the BLR code
fixes future crawls and leaves 135 LSEG rows sitting in `other`.

WHAT IT TOUCHES, AND WHAT IT REFUSES TO
---------------------------------------
Only `city`, and only on rows whose recomputed label actually differs. Every
lifecycle column — notified, status, first_seen_at, last_seen_at, miss_count,
and the five description-derived ones — is absent from the UPDATE, so this
cannot re-alert a job, reopen a closed one, or move a timestamp. `city` is
stored and used to decide nothing at crawl time; it is read later by the digest
filter, which is exactly why fixing it changes what you see without changing
what has happened.

IDEMPOTENT, BY CONSTRUCTION
---------------------------
The label is a pure function of the location string, and rows already carrying
the right label are skipped by the `IS DISTINCT FROM` comparison rather than
rewritten. Running it twice writes nothing the second time; running it after a
crawl that already labelled correctly writes nothing either.
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.connection import get_crawler_conn  # noqa: E402
from fetchers.normalize import city_label  # noqa: E402

LOAD = """
SELECT j.canonical_id, j.location, j.city, j.title, c.name AS company
  FROM jobs j
  JOIN companies c ON c.id = j.company_id
 ORDER BY c.name, j.canonical_id
"""

# `IS DISTINCT FROM` rather than `<>` so a row whose stored city is NULL is
# still updated. Only the one column is named.
UPDATE = """
UPDATE jobs
   SET city = $2
 WHERE canonical_id = $1
   AND city IS DISTINCT FROM $2
RETURNING canonical_id
"""

CHUNK = 200

# --- the fingerprint ------------------------------------------------------
#
# "The UPDATE only names `city`" is an argument. This is evidence: every column
# of the jobs table EXCEPT city, hashed per row, before the pass and again
# after. If the two digests match, nothing but the label moved — no timestamp
# nudged by a trigger, no `notified` cleared, no row inserted or deleted. If a
# future edit to this script reaches a second column by accident, the run says
# so instead of the next digest saying so by re-emailing 135 jobs.
#
# Ordered by canonical_id so the digest is a fact about the table's contents
# rather than about the order the pooler happened to return them in, and cast to
# text so a TIMESTAMPTZ renders identically on both reads.
FINGERPRINT = """
SELECT canonical_id,
       md5(
         concat_ws('|',
           company_id::text, title, location, url,
           source_posted_at::text, first_seen_at::text, last_seen_at::text,
           miss_count::text, status, notified::text,
           min_experience::text, is_pm_role::text, desc_checked::text,
           desc_fetch_failed::text, desc_checked_at::text
         )
       ) AS h
  FROM jobs
 ORDER BY canonical_id
"""

# The columns the fingerprint covers, for the report. `city` is the one column
# of the table deliberately absent from both this list and the digest above.
LIFECYCLE_COLUMNS = (
    "company_id", "title", "location", "url", "source_posted_at",
    "first_seen_at", "last_seen_at", "miss_count", "status", "notified",
    "min_experience", "is_pm_role", "desc_checked", "desc_fetch_failed",
    "desc_checked_at",
)


async def fingerprint(conn) -> tuple[str, dict[str, str]]:
    """One digest over every non-city column, plus the per-row hashes behind it.

    The rollup is what gets compared; the per-row map is what makes a mismatch
    diagnosable — "these three canonical_ids changed" rather than "something did".
    """
    rows = await conn.fetch(FINGERPRINT)
    per_row = {r["canonical_id"]: r["h"] for r in rows}
    rollup = hashlib.md5(
        "".join(f"{cid}:{h}" for cid, h in sorted(per_row.items())).encode("utf-8")
    ).hexdigest()
    return rollup, per_row


def report_fingerprint(before: tuple[str, dict[str, str]], after: tuple[str, dict[str, str]]) -> bool:
    """Print the before/after comparison. True when only `city` can have moved."""
    (before_hash, before_rows), (after_hash, after_rows) = before, after

    print("\n" + "-" * 72)
    print("LIFECYCLE FINGERPRINT  (every column except `city`)")
    print("-" * 72)
    print(f"  covering: {', '.join(LIFECYCLE_COLUMNS)}")
    print(f"  before:   {before_hash}  ({len(before_rows)} rows)")
    print(f"  after:    {after_hash}  ({len(after_rows)} rows)")

    if before_hash == after_hash:
        print("  MATCH — no column other than `city` changed, and no row was added or removed.")
        return True

    print("  MISMATCH — something other than `city` moved. Details:")
    for cid in sorted(set(before_rows) - set(after_rows))[:20]:
        print(f"    row disappeared: {cid}")
    for cid in sorted(set(after_rows) - set(before_rows))[:20]:
        print(f"    row appeared:    {cid}")
    changed = [c for c in sorted(set(before_rows) & set(after_rows)) if before_rows[c] != after_rows[c]]
    for cid in changed[:20]:
        print(f"    row altered:     {cid}")
    if len(changed) > 20:
        print(f"    ... and {len(changed) - 20} more altered")
    return False


async def main() -> int:
    dry_run = "--dry-run" in sys.argv[1:]

    conn = await get_crawler_conn()
    try:
        before = await fingerprint(conn)
        print(f"lifecycle fingerprint before: {before[0]} ({len(before[1])} rows)\n")

        rows = await conn.fetch(LOAD)
        print(f"recomputing city for {len(rows)} stored job(s)\n")

        changes = [
            (r, city_label(r["location"]))
            for r in rows
            if city_label(r["location"]) != r["city"]
        ]

        if not changes:
            print("Every stored row already carries the label the current rules give it.")
            # Still worth taking the second reading: on a re-run this is the
            # idempotence proof — nothing to write, and nothing written.
            report_fingerprint(before, await fingerprint(conn))
            return 0

        # Report BEFORE writing, broken down the way the brief asks: by the label
        # rows moved INTO, and by company.
        into: Counter[str] = Counter()
        moves: Counter[tuple[str, str]] = Counter()
        by_company: dict[str, Counter[tuple[str, str]]] = {}
        for row, new in changes:
            into[new] += 1
            moves[(row["city"], new)] += 1
            by_company.setdefault(row["company"], Counter())[(row["city"], new)] += 1

        print(f"{len(changes)} row(s) change label\n")
        print("  moving INTO:")
        for label, n in into.most_common():
            print(f"    {label:<14} {n:>5}")

        print("\n  by transition:")
        for (old, new), n in moves.most_common():
            print(f"    {old or 'NULL':<14} -> {new:<14} {n:>5}")

        print("\n  by company:")
        for company in sorted(by_company):
            parts = ", ".join(
                f"{old or 'NULL'}->{new} {n}" for (old, new), n in by_company[company].most_common()
            )
            total = sum(by_company[company].values())
            print(f"    {company[:14]:<14} {total:>5}   {parts}")

        print("\n  examples (raw location -> new label):")
        seen: set[tuple[str, str]] = set()
        for row, new in changes:
            key = (row["location"], new)
            if key in seen:
                continue
            seen.add(key)
            print(f"    {row['company'][:8]:<8} {(row['location'] or '')[:46]:<46} "
                  f"{row['city']:<7} -> {new}")
            if len(seen) >= 12:
                break

        if dry_run:
            print(f"\n--dry-run: {len(changes)} row(s) left untouched.")
            # The dry run's own claim, checked: it must not have written either.
            report_fingerprint(before, await fingerprint(conn))
            return 0

        # A transaction so a half-applied relabel cannot exist. The pass is
        # idempotent anyway, but "ran it and it died at row 900" should not be a
        # state anyone has to reason about.
        written = 0
        async with conn.transaction():
            for start in range(0, len(changes), CHUNK):
                for row, new in changes[start : start + CHUNK]:
                    if await conn.fetchval(UPDATE, row["canonical_id"], new):
                        written += 1

        print(f"\nupdated {written} row(s).")

        spread = await conn.fetch(
            "SELECT city, count(*) AS n FROM jobs GROUP BY city ORDER BY n DESC, city"
        )
        print("city labels now: " + ", ".join(f"{r['city']}={r['n']}" for r in spread))

        # Prove the pass converged: nothing should be left to change.
        rows = await conn.fetch(LOAD)
        remaining = [r for r in rows if city_label(r["location"]) != r["city"]]
        print(f"rows still disagreeing with the labeller: {len(remaining)}")

        # ...and prove it converged without touching anything else.
        clean = report_fingerprint(before, await fingerprint(conn))

        return 0 if (not remaining and clean) else 1
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
