"""Send one digest of matching, unnotified, open jobs. The real thing.

    python scripts/send_digest.py
    python scripts/send_digest.py --tag 30min      # label the notifications rows

Reads the preferences row, reads the notified=FALSE queue, filters, and — only
if something matched — sends exactly one email and marks exactly those jobs.

Zero matches is a success, not an error: nothing is sent and nothing is written.
A failed send is also a no-write; the jobs stay queued for the next run.

To see what this WOULD do without sending anything, use
scripts/preview_digest.py --dry-run instead.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.connection import get_crawler_conn  # noqa: E402
from notify.digest import DigestError, group_by_company, run_digest  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send the job-radar digest email.")
    parser.add_argument(
        "--tag",
        default="manual",
        help="suffix for the digest label written to notifications (default: manual)",
    )
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    conn = await get_crawler_conn()
    try:
        run = await run_digest(conn, tag_suffix=args.tag)
    finally:
        await conn.close()

    print(f"candidates considered : {run.candidates}  (notified=FALSE, status=open)")
    print(f"matched               : {len(run.matched)}")

    if not run.matched:
        print("emailed               : 0")
        print("\nno new matches")
        return 0

    for company, jobs in group_by_company(run.matched).items():
        print(f"  {company} ({len(jobs)})")
        for job in jobs:
            print(f"    - {job['title']}  [{job['city']}]")

    print(f"\nemailed               : {run.emailed}")
    print(f"subject               : {run.subject}")
    print(f"resend message id     : {run.message_id}")
    print(f"digest tag            : {run.tag}")

    if run.emailed != len(run.matched):
        print(
            f"\nWARNING: emailed {len(run.matched)} job(s) but only {run.emailed} row(s) "
            f"flipped to notified=TRUE. Investigate before the next run.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except DigestError as exc:
        print(f"\nFAILED (nothing was sent, nothing was written): {exc}", file=sys.stderr)
        raise SystemExit(1) from None
