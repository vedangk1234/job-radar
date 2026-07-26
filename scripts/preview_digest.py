"""See the digest without sending it. Never sends, never marks anything notified.

    python scripts/preview_digest.py --dry-run
    python scripts/preview_digest.py --dry-run --ignore-notified
    python scripts/preview_digest.py --dry-run --explain
    python scripts/preview_digest.py --dry-run --no-descriptions

Prints the matched jobs grouped by company and writes the rendered email to
digest_preview.html so it can be opened in a browser.

This script cannot send and cannot mark a job as sent. It does not import
record_sent, it never calls run_digest, and it holds no code that could set
notified=TRUE — the safety is structural rather than a flag that could be left
off by accident. --dry-run is accepted (and is the only mode) so that the
documented command reads honestly.

It is NOT, however, read-only any more, and pretending otherwise would be the
kind of stale docstring that gets believed. The second-stage filter reads each
title-stage candidate's description over the network and stores what it learned
in three columns — min_experience, is_pm_role, desc_checked. That write is why a
description is only ever fetched once, and it is the same write the real send
path performs. It touches nothing about a job's lifecycle: no notified flag, no
notifications row, and never the description text itself.

--no-descriptions skips the enrichment entirely for a genuinely read-only,
no-network run against whatever facts are already stored.

--ignore-notified drops the `notified = FALSE` clause from the candidate query.
That is the only way to exercise the matcher against a table whose rows were all
stored notified=TRUE by the silent seed: it answers "which of everything
currently open WOULD my filter pick".
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from db.connection import get_crawler_conn  # noqa: E402
from fetchers.http import close_client  # noqa: E402
from notify.description import enrich_candidates  # noqa: E402
from notify.digest import (  # noqa: E402
    group_by_company,
    render_html,
    render_text,
    subject_line,
)
from notify.match import explain, select_matches, title_stage_ok  # noqa: E402
from notify.store import fetch_candidates, load_preferences  # noqa: E402

DEFAULT_OUT = PROJECT_ROOT / "digest_preview.html"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Preview the digest. Never sends, never marks a job notified."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="accepted for clarity; no send is possible in any mode",
    )
    parser.add_argument(
        "--ignore-notified",
        action="store_true",
        help="consider every open job, not just the notified=FALSE queue (preview only)",
    )
    parser.add_argument(
        "--explain",
        action="store_true",
        help="also list the rejected candidates and why each was dropped",
    )
    parser.add_argument(
        "--no-descriptions",
        action="store_true",
        help="skip the description fetch; judge on the facts already stored (no network, no writes)",
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="where to write the HTML")
    return parser.parse_args()


def print_second_stage(title_stage, prefs, read_this_run: set[str]) -> None:
    """Every title-stage candidate, its two stored facts, and the verdict.

    Printed for all of them rather than only the ones just fetched, because "we
    already knew this one" is not a different outcome — desc_checked simply means
    the answer came from the row instead of from the network.
    """
    kept = {job["canonical_id"] for job in select_matches(title_stage, prefs)}
    print(f"\nsecond stage (description-derived facts, {len(title_stage)} candidates):")
    for job in title_stage:
        report = explain(job, prefs)
        years = report.min_experience
        source = "read now" if job["canonical_id"] in read_this_run else "already stored"
        print(
            f"  {'KEPT   ' if job['canonical_id'] in kept else 'DROPPED'}"
            f"  min_experience={('null' if years is None else str(years)):<5}"
            f" is_pm_role={str(report.is_pm_role).lower():<5}  ({source})"
        )
        print(f"           {job.get('company')} — {job.get('title')}")
        print(f"           {report.reason}")


async def main() -> int:
    args = parse_args()

    conn = await get_crawler_conn()
    try:
        prefs = await load_preferences(conn)
        candidates = await fetch_candidates(conn, ignore_notified=args.ignore_notified)

        # Stage 1 (title) decides who is worth a request; stage 2 (description)
        # answers only for those. The order is the whole reason this is cheap.
        title_stage = [job for job in candidates if title_stage_ok(job, prefs)]
        enriched: list[dict] = []
        if not args.no_descriptions:
            enriched = await enrich_candidates(conn, title_stage)
    finally:
        await conn.close()
        await close_client()

    scope = "every open job" if args.ignore_notified else "notified=FALSE and open"
    print(f"preferences: {len(prefs.cities)} cities, {len(prefs.keywords)} keywords, "
          f"{len(prefs.exclusions)} exclusions")
    print(f"  cities     : {', '.join(prefs.cities)}")
    print(f"candidates considered : {len(candidates)}  ({scope})")
    print(f"title-stage candidates: {len(title_stage)}  (city + keyword + no exclusion + level ok)")

    if args.no_descriptions:
        print("descriptions read     : 0  (--no-descriptions)")
    else:
        print(f"descriptions read     : {len(enriched)}  "
              f"(of {len(title_stage)}; the rest were already desc_checked=TRUE)")
        unavailable = [e for e in enriched if not e["fetched"]]
        if unavailable:
            print(f"  descriptions unavailable for {len(unavailable)} — kept, per the "
                  f"inclusion bias: " + ", ".join(e["canonical_id"] for e in unavailable))

    matched = select_matches(candidates, prefs)
    if title_stage:
        print_second_stage(title_stage, prefs, {e["canonical_id"] for e in enriched})
    print(f"\nmatched               : {len(matched)}\n")

    if not matched:
        print("no new matches — a real run would send nothing and exit 0")
        print_footer(len(enriched))
        return 0

    groups = group_by_company(matched)
    for company, jobs in groups.items():
        print(f"{company} ({len(jobs)})")
        for job in jobs:
            print(f"  - {job['title']}")
            print(f"      {(job['city'] or '?'):<14} {job['location'] or '(no location)'}")
            print(f"      {job['url']}")
        print()

    cities = Counter(job["city"] for job in matched)
    print(f"companies             : {len(groups)}")
    print("city spread           : " + ", ".join(f"{c}={n}" for c, n in cities.most_common()))
    print(f"subject would be      : {subject_line(matched)}")

    if args.explain:
        kept = {job["canonical_id"] for job in matched}
        rejected = [explain(j, prefs) for j in candidates if j["canonical_id"] not in kept]
        print(f"\nrejected ({len(rejected)}):")
        for report in rejected:
            print(f"  - {report.job['title'][:58]:<58} {report.reason}")

    args.out.write_text(render_html(matched, prefs), encoding="utf-8")
    print(f"\nHTML written to       : {args.out}")
    print(f"plain-text fallback   : {len(render_text(matched, prefs).splitlines())} lines")
    print_footer(len(enriched))
    return 0


def print_footer(descriptions_read: int) -> None:
    """State exactly what this run did and did not write. No rounding up."""
    print("\nDRY RUN — nothing sent, no notifications row, notified flags untouched.")
    if descriptions_read:
        print(
            f"          {descriptions_read} row(s) had min_experience, is_pm_role and "
            f"desc_checked written.\n"
            f"          No description text was stored — there is no column for it."
        )


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
