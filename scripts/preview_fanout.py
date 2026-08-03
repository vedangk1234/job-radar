"""See what every user's digest would contain. Never sends, never records.

    python scripts/preview_fanout.py --dry-run
    python scripts/preview_fanout.py --dry-run --no-descriptions

scripts/preview_digest.py answers "what would MY digest contain" in detail — the
rejected candidates, the second-stage verdicts, the rendered HTML. This one
answers the question that only exists once there is a fan-out: does each user get
their own matches, and only their own?

So the output is shaped for comparison rather than for reading one digest: per
user, their counts and their matched jobs, then the OVERLAP REPORT — which jobs
appear in more than one user's digest, and which appear in exactly one. A job at
a company two people watch SHOULD appear twice, once per person, because they are
two independent decisions about the same posting. A job at a company only one of
them watches appearing in both would be the fan-out leaking, and it is the thing
this script exists to make visible.

CANNOT SEND AND CANNOT RECORD, structurally rather than by a flag: it does not
import run_fan_out, run_digest or record_sent, and holds no statement that writes
notification state. It is the same discipline scripts/preview_digest.py follows.

It is NOT read-only, for the one reason that script names too: the second-stage
filter reads each title-stage candidate's description over the network and stores
what it learned in five columns. That write says nothing about whether a job was
sent. --no-descriptions skips it entirely for a genuinely read-only run.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from db.connection import get_crawler_conn  # noqa: E402
from db.users import digest_users  # noqa: E402
from db.watchlist import count_tracked  # noqa: E402
from fetchers.http import close_client  # noqa: E402
from notify.description import enrich_candidates  # noqa: E402
from notify.digest import group_by_company, subject_line  # noqa: E402
from notify.match import select_matches, title_stage_ok  # noqa: E402
from notify.store import fetch_candidates, load_preferences  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Preview every user's digest. Never sends, never records."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="accepted for clarity; no send is possible in any mode",
    )
    parser.add_argument(
        "--no-descriptions",
        action="store_true",
        help="skip the description fetch; judge on the facts already stored (no network, no writes)",
    )
    return parser.parse_args()


async def preview_one(conn, user_id, email: str, *, read_descriptions: bool) -> list[dict]:
    """One user's digest, computed exactly as run_digest computes it, minus the send.

    The same three calls in the same order — their preferences, their queue,
    their filter with the enrichment between its two stages. Anything this
    reproduces by hand instead of importing would be a preview of a different
    program; the send and the receipt are the only things left out, because they
    are the only things that write.
    """
    prefs = await load_preferences(conn, user_id)
    candidates = await fetch_candidates(conn, user_id)
    tracked = await count_tracked(conn, user_id)

    title_stage = [job for job in candidates if title_stage_ok(job, prefs)]
    if read_descriptions:
        # No re-read afterwards, and that is not an omission: enrich_candidates
        # updates each row dict in place precisely so the caller's next match
        # sees the facts it just learned. select_matches below therefore reads
        # the same objects run_digest reads at the same point.
        await enrich_candidates(conn, title_stage)

    matched = select_matches(candidates, prefs)

    print(f"\n{email}")
    print(f"  companies tracked     : {tracked}")
    print(f"  cities                : {', '.join(prefs.cities)}")
    print(f"  keywords / exclusions : {len(prefs.keywords)} / {len(prefs.exclusions)}"
          f"   max_level {prefs.max_level}")
    print(f"  candidates considered : {len(candidates)}  (open, watched, not yet sent to them)")
    print(f"  title-stage           : {len(title_stage)}")
    print(f"  matched               : {len(matched)}")

    if not matched:
        print("  would send            : nothing (a real run sends no email at all)")
        return []

    for company, jobs in group_by_company(matched).items():
        print(f"    {company} ({len(jobs)})")
        for job in jobs:
            print(f"      - {job['title']}  [{job['city']}]")
    print(f"  subject would be      : {subject_line(matched)}")
    return [dict(job) for job in matched]


def print_overlap(per_user: dict[str, list[dict]]) -> None:
    """Who would receive what, from the job's side rather than the user's.

    The one report a per-user listing cannot give you. Two users watching the
    same company SHOULD both see its matching jobs — that is "crawl once, fan
    out" working, one fetch and two independent decisions. What would be wrong is
    a job reaching somebody whose watch list does not contain its company, and
    that shows up here as a shared row where only one user tracks the employer.
    """
    owners: dict[str, list[str]] = defaultdict(list)
    company_of: dict[str, str] = {}
    for email, jobs in per_user.items():
        for job in jobs:
            owners[job["canonical_id"]].append(email)
            company_of[job["canonical_id"]] = job.get("company") or "(unknown)"

    shared = {cid: who for cid, who in owners.items() if len(who) > 1}
    print(f"\noverlap: {len(owners)} distinct job(s) across all digests, "
          f"{len(shared)} in more than one")
    for cid, who in sorted(shared.items()):
        print(f"  {company_of[cid]:<24} {cid}")
        print(f"    -> {', '.join(sorted(who))}")
    if not shared:
        print("  (no job would reach two inboxes — the watch lists are disjoint here)")


async def main() -> int:
    args = parse_args()

    conn = await get_crawler_conn()
    try:
        users = await digest_users(conn)
        if not users:
            print("no active users — a real run would send nothing")
            return 0

        print(f"fanning out to {len(users)} active user(s) …")
        per_user: dict[str, list[dict]] = {}
        for user_id, email in users:
            per_user[email] = await preview_one(
                conn, user_id, email, read_descriptions=not args.no_descriptions
            )
    finally:
        await conn.close()
        await close_client()

    print_overlap(per_user)

    total = sum(len(jobs) for jobs in per_user.values())
    would_send = sum(1 for jobs in per_user.values() if jobs)
    print(
        f"\nDRY RUN — nothing sent, no notifications row, no user_job_notifications "
        f"row, no flag touched."
    )
    print(f"          a real run would send {would_send} email(s) carrying "
          f"{total} job notification(s) in total.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
