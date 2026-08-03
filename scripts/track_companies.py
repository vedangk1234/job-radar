"""Start watching companies for a user — without delivering their history.

    python scripts/track_companies.py --email me@example.com --company-ids 3,17,42
    python scripts/track_companies.py --email me@example.com --all
    python scripts/track_companies.py --email me@example.com --all --dry-run

The command-line form of the action the Phase-3 dashboard picker will perform,
and the only supported way to create a tracking row. It exists as a script rather
than as raw SQL for one reason: an INSERT into user_companies is HALF of what
"track this company" means.

WHY A PLAIN INSERT IS THE WRONG THING TO RUN. Since Phase 1e the digest queue is
"open, in a company you watch, not back catalogue, not yet sent to you". A
company in the catalog has stored jobs — hundreds, open, ordinary, never emailed
to anyone — and the instant a tracking row exists every one of them is in that
person's queue. Their first digest would be an archive dump, which is the
cry-wolf failure this whole codebase is biased against. jobs.seeded covers a
company's FIRST-EVER crawl and says nothing about the weeks of ordinary crawling
after it. So db/watchlist.track_companies writes the pair and records that
company's stored jobs as already-notified for that user, in one transaction, and
this script is a thin argument parser over it.

--email rather than MY_EMAIL, unlike every migration script here. This one acts
on a NAMED person: it is what you run to set up or inspect a second account, and
defaulting the target to whoever the environment belongs to is how you would
quietly configure your own. The address is still resolved through public.users,
so it must already exist (scripts/seed_user.py creates one).

IDEMPOTENT. A company already tracked yields no insert and therefore no
suppression — see track_companies, where that distinction is what stops a second
run swallowing the jobs that arrived since the first.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.connection import get_crawler_conn  # noqa: E402
from db.notifications import count_notified_for_user  # noqa: E402
from db.users import CURRENT_USER_SQL  # noqa: E402
from db.watchlist import (  # noqa: E402
    catalog_company_ids,
    count_tracked,
    track_companies,
    tracked_company_ids,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Track companies for a user, suppressing their stored history."
    )
    parser.add_argument("--email", required=True, help="the public.users address to act on")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true", help="track every company in the catalog")
    group.add_argument("--company-ids", help="comma-separated companies.id values")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be tracked and change nothing",
    )
    return parser.parse_args()


def parse_ids(raw: str) -> list[int]:
    """'3, 17,42' -> [3, 17, 42], or a clear error naming the bad token."""
    ids = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        if not token.isdigit():
            raise SystemExit(f"FAILED: {token!r} is not a company id")
        ids.append(int(token))
    if not ids:
        raise SystemExit("FAILED: --company-ids listed nothing")
    return ids


async def main() -> int:
    args = parse_args()
    email = args.email.strip().lower()

    conn = await get_crawler_conn()
    try:
        user_id = await conn.fetchval(CURRENT_USER_SQL, email)
        if user_id is None:
            print(
                f"FAILED: no public.users row for {email}. Create it first "
                f"(scripts/seed_user.py reads MY_EMAIL) — this script will not "
                f"invent an account to attach a watch list to.",
                file=sys.stderr,
            )
            return 1

        catalog = await catalog_company_ids(conn)
        wanted = catalog if args.all else parse_ids(args.company_ids)

        unknown = sorted(set(wanted) - set(catalog))
        if unknown:
            print(
                f"FAILED: no such company id(s): {unknown}. Nothing was tracked — "
                f"the foreign key would refuse them one at a time and leave a "
                f"partial watch list behind.",
                file=sys.stderr,
            )
            return 1

        already = set(await tracked_company_ids(conn, user_id))
        new = [cid for cid in wanted if cid not in already]

        print(f"user                  : {email}")
        print(f"catalog               : {len(catalog)} company/ies")
        print(f"already tracked       : {len(already)}")
        print(f"would newly track     : {len(new)}  {new if len(new) <= 20 else ''}")

        if args.dry_run:
            print("\nDRY RUN — nothing tracked, no history suppressed.")
            return 0

        before = await count_notified_for_user(conn, user_id)
        tracked = await track_companies(conn, user_id, wanted)
        after = await count_notified_for_user(conn, user_id)
        total = await count_tracked(conn, user_id)
    finally:
        await conn.close()

    print(f"\nnewly tracked         : {len(tracked)}")
    print(f"now watching          : {total} company/ies")
    print(f"history suppressed    : {after - before} job(s) recorded as already-notified")
    print(
        "\nThose jobs will never appear in this user's digest. That is the point: "
        "a watch list starts a subscription, not a backfill of everything the "
        "crawler has ever stored for that employer."
    )

    if len(tracked) != len(new):
        print(
            f"\nWARNING: expected to track {len(new)} company/ies but tracked "
            f"{len(tracked)}. Something inserted or removed a tracking row while "
            f"this ran. Re-run — it is idempotent — and check the counts agree.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception as exc:  # noqa: BLE001 - top-level CLI error reporting
        print(f"\nFAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
