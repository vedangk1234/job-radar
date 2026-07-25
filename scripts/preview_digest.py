"""See the digest without sending it. Read-only, always.

    python scripts/preview_digest.py --dry-run
    python scripts/preview_digest.py --dry-run --ignore-notified
    python scripts/preview_digest.py --dry-run --explain

Prints the matched jobs grouped by company and writes the rendered email to
digest_preview.html so it can be opened in a browser.

This script has no write path. It does not import record_sent, it never calls
run_digest, and it holds no code that could set notified=TRUE — the safety is
structural rather than a flag that could be left off by accident. --dry-run is
accepted (and is the only mode) so that the documented command reads honestly.

--ignore-notified drops the `notified = FALSE` clause from the candidate query.
That is the only way to exercise the matcher against a table whose rows were all
stored notified=TRUE by the silent seed: it answers "which of everything
currently open WOULD my filter pick", without touching a single row.
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
from notify.digest import (  # noqa: E402
    group_by_company,
    render_html,
    render_text,
    subject_line,
)
from notify.match import explain, select_matches  # noqa: E402
from notify.store import fetch_candidates, load_preferences  # noqa: E402

DEFAULT_OUT = PROJECT_ROOT / "digest_preview.html"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Preview the digest. Never sends, never writes.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="accepted for clarity; this script is read-only in every mode",
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
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="where to write the HTML")
    return parser.parse_args()


async def main() -> int:
    args = parse_args()

    conn = await get_crawler_conn()
    try:
        prefs = await load_preferences(conn)
        candidates = await fetch_candidates(conn, ignore_notified=args.ignore_notified)
    finally:
        await conn.close()

    scope = "every open job" if args.ignore_notified else "notified=FALSE and open"
    print(f"preferences: {len(prefs.cities)} cities, {len(prefs.keywords)} keywords, "
          f"{len(prefs.exclusions)} exclusions")
    print(f"  cities     : {', '.join(prefs.cities)}")
    print(f"candidates considered : {len(candidates)}  ({scope})")

    matched = select_matches(candidates, prefs)
    print(f"matched               : {len(matched)}\n")

    if not matched:
        print("no new matches — a real run would send nothing and exit 0")
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
    print("\nDRY RUN — nothing sent, no rows written, notified flags untouched.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
