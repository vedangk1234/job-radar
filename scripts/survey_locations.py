"""What do the location strings we are THROWING AWAY actually say?

    python scripts/survey_locations.py                 # the `other` + `india-unclear` buckets
    python scripts/survey_locations.py --label other   # one bucket
    python scripts/survey_locations.py --recompute     # label with the CURRENT labeller
    python scripts/survey_locations.py --min 2         # only strings seen twice or more
    python scripts/survey_locations.py --open          # live rows only

WHY THIS EXISTS
---------------
`other` is the one label the default preferences drop, and `india-unclear` is
the bucket a real city falls into when its name is spelled in a way the labeller
does not recognise. Both are where a missed job goes to die — silently, because
a job that never reaches a digest produces no error and no row anyone looks at.

Every rule in fetchers.normalize._CITY_MARKERS should be able to name the
strings it was added for. This script is how those strings are found: it prints
the raw location text, verbatim and grouped by company, so a spelling variant
("Banglore") shows up as evidence rather than as a guess. Adding markers from
imagination is how substring false-positives get written; adding them from this
output is how they do not.

STORED LABEL vs RECOMPUTED LABEL
--------------------------------
By default the buckets are the `city` column as stored — that is what the digest
actually filters on today, so it is what "currently labelled" means.

`--recompute` re-runs fetchers.normalize.city_label over the raw location
instead, which answers a different and equally useful question: what WOULD the
current rules say? Before scripts/relabel_cities.py has run, the two disagree by
exactly the rows that pass is waiting to fix, and the difference is printed.

This script never writes anything.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.connection import get_crawler_conn  # noqa: E402
from fetchers.normalize import city_label  # noqa: E402

LOAD = """
SELECT j.canonical_id, j.location, j.city, j.status, c.name AS company
  FROM jobs j
  JOIN companies c ON c.id = j.company_id
 ORDER BY c.name, j.location
"""

DEFAULT_LABELS = ("other", "india-unclear")


def _display(location: str | None) -> str:
    """The raw string, made visible — NULL and empty are different facts."""
    if location is None:
        return "<NULL>"
    if not location.strip():
        return "<EMPTY>"
    return location


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--label",
        default=",".join(DEFAULT_LABELS),
        help="comma-separated labels to survey (default: other,india-unclear)",
    )
    ap.add_argument(
        "--recompute",
        action="store_true",
        help="bucket by what the CURRENT labeller says, not by the stored city column",
    )
    ap.add_argument("--min", type=int, default=1, help="hide strings seen fewer than N times")
    ap.add_argument("--open", action="store_true", help="live rows only (status='open')")
    args = ap.parse_args()

    labels = tuple(l.strip() for l in args.label.split(",") if l.strip())

    conn = await get_crawler_conn()
    try:
        rows = await conn.fetch(LOAD)
    finally:
        await conn.close()

    if args.open:
        rows = [r for r in rows if r["status"] == "open"]

    source = "recomputed by the current labeller" if args.recompute else "the stored city column"
    print(f"{len(rows)} job row(s){' (open only)' if args.open else ''}; bucketing by {source}")

    def label_of(row) -> str:
        return city_label(row["location"]) if args.recompute else (row["city"] or "")

    # How far the stored labels have drifted from the rules as they stand now.
    # Non-zero means scripts/relabel_cities.py has work to do.
    stale = [r for r in rows if (r["city"] or "") != city_label(r["location"])]
    print(f"rows whose stored city disagrees with the current labeller: {len(stale)}")
    if stale:
        drift: Counter[tuple[str, str]] = Counter(
            ((r["city"] or "NULL"), city_label(r["location"])) for r in stale
        )
        for (old, new), n in drift.most_common():
            print(f"    {old:<14} -> {new:<14} {n:>5}")

    hits = [r for r in rows if label_of(r) in labels]
    print(f"\nsurveying {len(hits)} row(s) labelled {' / '.join(labels)}\n")
    if not hits:
        return 0

    # --- grouped by company, which is what makes a per-tenant format visible ---
    # A misspelling is almost never spread evenly across boards; it belongs to
    # one company's ATS tenant and repeats there. Grouping by company is what
    # turns "3 odd rows" into "PhonePe writes it this way every time".
    by_company: dict[str, Counter[tuple[str, str]]] = {}
    for row in hits:
        key = (_display(row["location"]), label_of(row))
        by_company.setdefault(row["company"], Counter())[key] += 1

    print("=" * 100)
    print("BY COMPANY")
    print("=" * 100)
    for company in sorted(by_company, key=lambda c: (-sum(by_company[c].values()), c)):
        strings = by_company[company]
        total = sum(strings.values())
        print(f"\n{company}  ({total} row(s), {len(strings)} distinct string(s))")
        for (location, label), n in sorted(strings.items(), key=lambda kv: (-kv[1], kv[0])):
            if n < args.min:
                continue
            print(f"    {n:>5}  [{label:<13}]  {location}")

    # --- the same strings, deduplicated across companies -----------------------
    # The per-company view says who; this one says how big. A variant that shows
    # up under three tenants is worth a rule more than one that shows up under
    # one, and only this view makes that visible.
    distinct: Counter[tuple[str, str]] = Counter()
    companies_of: dict[tuple[str, str], set[str]] = {}
    for company, strings in by_company.items():
        for key, n in strings.items():
            distinct[key] += n
            companies_of.setdefault(key, set()).add(company)

    shown = [(k, n) for k, n in distinct.most_common() if n >= args.min]
    print("\n" + "=" * 100)
    print(f"DISTINCT LOCATION STRINGS  ({len(distinct)} distinct, {len(shown)} shown at --min {args.min})")
    print("=" * 100)
    for (location, label), n in shown:
        who = ", ".join(sorted(companies_of[(location, label)]))
        print(f"{n:>5}  [{label:<13}]  {location}")
        print(f"       {who[:110]}")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
