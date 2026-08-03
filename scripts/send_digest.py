"""Send one digest per user, each containing only that user's matches.

    python scripts/send_digest.py
    python scripts/send_digest.py --tag 30min      # label the notifications rows

For every active account: read their preferences, read their queue — every open
job in a company they watch, not stored by a silent seed, that they have not been
emailed about — filter it, and, only if something matched, send exactly one email
and record exactly those jobs against that user.

This is the notify half of "crawl once, fan out". The crawl above it is
user-blind and stays that way: it fetched every tracked board once, for
everybody. All the narrowing is here.

Zero matches for a user is a success, not an error: nothing is sent and nothing
is written for them. A failed send is also a no-write, and is recorded against
that one user rather than ending the run — the other accounts still get their
digest, and the exit code is non-zero so the failure cannot pass as a quiet hour.

To see what this WOULD do without sending anything, use
scripts/preview_fanout.py --dry-run (everyone) or scripts/preview_digest.py
--dry-run (just the MY_EMAIL account).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.connection import get_crawler_conn  # noqa: E402
from notify.digest import DigestError, group_by_company, run_fan_out  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send the job-radar digests.")
    parser.add_argument(
        "--tag",
        default="manual",
        help="suffix for the digest label written to notifications (default: manual)",
    )
    return parser.parse_args()


def print_user(email: str, run) -> None:
    """One account's outcome, in the shape the single-user run always printed."""
    print(f"\n{email}")
    print(f"  candidates considered : {run.candidates}  (open, watched, not yet sent to them)")
    print(f"  matched               : {len(run.matched)}")

    if run.error:
        print(f"  FAILED                : {run.error}  (nothing sent, nothing written)")
        return

    if not run.matched:
        print("  emailed               : 0  — no new matches")
        return

    for company, jobs in group_by_company(run.matched).items():
        print(f"    {company} ({len(jobs)})")
        for job in jobs:
            print(f"      - {job['title']}  [{job['city']}]")

    print(f"  emailed               : {run.emailed}")
    print(f"  subject               : {run.subject}")
    print(f"  resend message id     : {run.message_id}")
    print(f"  digest tag            : {run.tag}")


async def main() -> int:
    args = parse_args()
    conn = await get_crawler_conn()
    try:
        fan_out = await run_fan_out(conn, tag_suffix=args.tag)
    finally:
        await conn.close()

    if not fan_out.runs:
        print("no active users — nothing to send")
        return 0

    for _, email, run in fan_out.runs:
        print_user(email, run)

    print(
        f"\n{len(fan_out.runs)} user(s) considered, {fan_out.sent} digest(s) sent, "
        f"{fan_out.emailed} job(s) recorded"
    )

    status = 0

    # A gap between what was emailed and what was recorded means those jobs are
    # still queued and go out again next run. Reported per user, because the
    # number that matters is the one belonging to the inbox it will arrive in.
    for _, email, run in fan_out.runs:
        if run.error is None and run.emailed != len(run.matched):
            print(
                f"\nWARNING: emailed {len(run.matched)} job(s) to {email} but only "
                f"{run.emailed} user_job_notifications row(s) were written. Any job "
                f"in that gap will be emailed again on the next run. Investigate "
                f"before then.",
                file=sys.stderr,
            )
            status = 1

    if fan_out.failures:
        print(
            f"\nFAILED for {len(fan_out.failures)} of {len(fan_out.runs)} user(s): "
            + ", ".join(f"{email} ({run.error})" for _, email, run in fan_out.failures)
            + "\n        Nothing was sent or written for them; every other account "
            "was unaffected. Their jobs stay queued for the next run.",
            file=sys.stderr,
        )
        status = 1

    return status


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except DigestError as exc:
        # Only reachable for a failure that is NOT per-user — run_fan_out catches
        # those and reports them above.
        print(f"\nFAILED (nothing was sent, nothing was written): {exc}", file=sys.stderr)
        raise SystemExit(1) from None
