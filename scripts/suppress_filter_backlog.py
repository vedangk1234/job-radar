"""Record every account's existing back catalogue as already-seen, once.

EVERY ACCOUNT WITH A FILTER, ACTIVE OR NOT — see ACCOUNTS_SQL, which is the one
place this differs from db.users.digest_users and the difference is load bearing.

    python scripts/suppress_filter_backlog.py --dry-run  # report only, write nothing
    python scripts/suppress_filter_backlog.py            # apply

WHY THIS IS NEEDED, AND WHY IT MUST RUN BEFORE THE PUSH
-------------------------------------------------------
The filter-first slice removed one clause from notify/store.CANDIDATES_SQL:

    AND EXISTS (user_companies uc WHERE uc.user_id = $1
                                    AND uc.company_id = j.company_id)

For an account that already watches the whole catalog that clause was always
true, so removing it changes nothing. For every other account it was doing real
work, and removing it exposes — in one instant — every open, non-seeded job at a
company they do NOT watch that their filter happens to match. None of those rows
has a user_job_notifications row, because rows are written when something is
SENT and nothing was ever sent for a job that did not reach the queue. So nothing
suppresses them, and the very first cron after the deploy emails the lot as news
because a WHERE clause changed.

This is the same hazard scripts/relabel_cities.report_blast_radius refuses to
apply on, reached from the third side: there a LABELLER moved a job into a
filter, in lib/preferences.savePreferences a PERSON moves their filter onto a
job, and here a DEPLOY moves the queue onto both.

ORDERING: THIS RUNS FIRST, THEN THE PUSH. That is the opposite of the pending
cities item ("push, then relabel") and the difference is worth stating because
getting it backwards is an inbox event either way. The relabel has to go second
because crawler/store.UPSERT_SEEN rewrites `city` on every re-sighting, so a
relabel applied before the new labeller ships is undone within the hour. Nothing
rewrites user_job_notifications, so this can go first — and it MUST, because the
rows it writes are invisible to the deployed queue (they are outside the
watch-list gate it still applies) right up until the moment that gate is gone.
Run it before the push and the push is a no-op for every inbox; run it after and
the gap between them is a digest.

WHAT IT WRITES, AND WHAT IT REFUSES TO
--------------------------------------
Rows in user_job_notifications, and nothing else. `jobs` is not named in any
statement here, which is an argument; the lifecycle fingerprint below is the
evidence, hashing every column of every job row before and after and refusing to
report success if the two differ.

THE SET IS THE QUEUE'S OWN QUESTION, ASKED OF THE NEW QUEUE.
notify/store.suppress_filter_backlog is what computes it — the same function
lib/preferences.savePreferences calls on every save — with `before=None`, meaning
"this account has no prior filter to subtract", so the delta is everything its
current filter admits. That is deliberate: a migration that reimplemented the
rule would prove its own reimplementation. It is status-blind for the reason
stated there, so a closed job that reopens cannot arrive as news.

IDEMPOTENT, BY CONSTRUCTION. The insert is ON CONFLICT DO NOTHING and the read
excludes anything already recorded, so a second run selects nothing and writes
nothing. Interrupt it and run it again.

DELIBERATELY NOT SCOPED TO WHAT THE OLD QUEUE COULD SEE, and that is the one
choice here that could go the other way. It suppresses everything the account's
filter currently matches, including rows that were ALREADY in their queue under
the watch-list gate — jobs at companies they watch, matching, unsent. Those are
legitimately owed to them and this withholds them.

That is the right call for exactly one reason: this project's expensive failure
is crying wolf, not missing a job. The alternative — suppress only rows outside
the old gate — needs the old gate to still be computable at the moment the script
runs, which makes the migration depend on a clause the commit it accompanies has
deleted. A person who loses one hour's genuinely-new matches gets a quiet digest;
a person who gets the wrong side of that trade gets four hundred emails. The
report prints both numbers so the cost is on the record rather than implied.
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.connection import get_crawler_conn  # noqa: E402
from notify.store import (  # noqa: E402
    fetch_suppression_candidates,
    load_preferences,
    suppress_filter_backlog,
)
from notify.match import is_match  # noqa: E402

# The whole jobs table, hashed per row. "This script does not touch jobs" is an
# argument; this is evidence. Ordered by canonical_id so the digest is a fact
# about contents rather than about the order the pooler returned them in, and
# cast to text so a TIMESTAMPTZ renders identically on both reads.
#
# EVERY COLUMN, unlike relabel_cities' fingerprint which excludes the one it
# means to change. This script changes NO column of jobs, so the fingerprint has
# no exception in it — which makes it a stronger instrument than that one, and it
# is the right shape here precisely because the thing being asserted is total.
FINGERPRINT = """
SELECT canonical_id,
       md5(
         concat_ws('|',
           company_id::text, title, location, url, city,
           source_posted_at::text, first_seen_at::text, last_seen_at::text,
           miss_count::text, status, seeded::text,
           min_experience::text, is_pm_role::text, desc_checked::text,
           desc_fetch_failed::text, desc_checked_at::text,
           work_mode, salary_min::text, salary_max::text, salary_checked::text,
           applied::text, applied_at::text
         )
       ) AS h
  FROM jobs
 ORDER BY canonical_id
"""

# What the account's queue holds RIGHT NOW, under the clause the deployed digest
# still applies. Read only to report the cost — see the docstring's last
# paragraph — and never to decide anything.
OLD_QUEUE_SQL = """
SELECT j.canonical_id
  FROM jobs j
 WHERE j.status = 'open'
   AND EXISTS (
         SELECT 1
           FROM user_companies uc
          WHERE uc.user_id = $1
            AND uc.company_id = j.company_id
       )
   AND j.seeded = FALSE
   AND NOT EXISTS (
         SELECT 1
           FROM user_job_notifications n
          WHERE n.user_id = $1
            AND n.canonical_id = j.canonical_id
       )
"""

COMPANY_NAMES = "SELECT id, name FROM companies"

# EVERY ACCOUNT THAT HAS A FILTER — NOT db.users.digest_users, AND THE DIFFERENCE
# IS THE WHOLE POINT OF THIS CONSTANT.
#
# digest_users reads `is_active = TRUE`, because that is the account-level off
# switch for SENDING. Using it here would be the obvious choice and it would be
# wrong, because what this pass guards against is not "who gets an email tonight"
# but "whose back catalogue is exposed the moment the gate goes". Those come apart
# on exactly the account that matters: a signup is created `is_active = FALSE`
# (lib/link-user branch 3, because the sandbox sender would turn the cron red), so
# a person who has completed onboarding and is waiting on 2d has a real filter, a
# real match set, and no suppression — and would receive the entire archive in
# their first-ever digest on the day somebody flips the flag, weeks after this
# migration ran and looked clean.
#
# The live database has such an account today, which is how this was found rather
# than reasoned about: 5 of 50 companies watched, is_active FALSE, a real filter,
# and a catalog-wide backlog nothing had ever recorded.
#
# THE TWO QUESTIONS ARE DIFFERENT AND MUST NOT BE UNIFIED, which is the whole of
# why this constant exists beside digest_users rather than calling it:
#
#     digest_users   "who is emailed RIGHT NOW"          -> is_active = TRUE
#     ACCOUNTS_SQL   "whose suppression must already be   -> has a filter at all
#                     true BEFORE they can ever be
#                     emailed"
#
# The suppression is a PRECONDITION, not a delivery. It has to hold for everybody
# who could ever become a recipient, and `is_active` is precisely the switch that
# turns a non-recipient into one — so scoping the precondition by it makes the
# guarantee evaporate at the exact moment it is needed. A future edit that "tidies"
# these into one call re-opens the hole silently and weeks late.
#
# This is the same shape as notify/store.load_active_preferences being broader
# than load_preferences: the crawl enriches against the UNION of filters because a
# description read serves anybody who can see the row, while a digest reads one
# person's. Narrow reads decide a send; wide reads establish a precondition. Same
# distinction, one table over.
#
# So the set is "has a preferences row", and inactive accounts are reported with
# their status shown rather than skipped.
ACCOUNTS_SQL = """
SELECT u.id, u.email, u.is_active, p.onboarding_completed
  FROM public.users u
  JOIN preferences p ON p.user_id = u.id
 ORDER BY lower(u.email)
"""


async def fingerprint(conn) -> tuple[str, int]:
    rows = await conn.fetch(FINGERPRINT)
    digest = hashlib.md5("".join(f"{r['canonical_id']}:{r['h']}" for r in rows).encode()).hexdigest()
    return digest, len(rows)


def report_fingerprint(before: tuple[str, int], after: tuple[str, int]) -> bool:
    print("\n" + "-" * 72)
    print("JOBS LIFECYCLE FINGERPRINT")
    print("-" * 72)
    print(f"  before: {before[0]}  ({before[1]} rows)")
    print(f"  after : {after[0]}  ({after[1]} rows)")
    if before == after:
        print("  MATCH — not one column of one job row moved.")
        return True
    print("  MISMATCH — something wrote to `jobs` during this run.")
    print("  If the hourly crawl landed mid-run that is a fact about this database")
    print("  rather than about this script, which names `jobs` in no UPDATE at all.")
    print("  Re-run against a quiet minute before believing anything else here.")
    return False


async def report_account(
    conn, user_id, email: str, active: bool, onboarded: bool, companies: dict[int, str]
) -> list[str]:
    """Everything this account's filter admits that nothing has recorded yet.

    Returns the canonical_ids. Prints the entering set grouped by company, the
    open/closed split, and the rows that were ALREADY queued under the deployed
    gate — the ones this pass costs them.
    """
    prefs = await load_preferences(conn, user_id)
    candidates = await fetch_suppression_candidates(conn, user_id)
    entering = [row for row in candidates if is_match(row, prefs)]

    # `status` is deliberately NOT in the candidate SELECT list — the suppression
    # read is status-blind, so the column decides nothing and carrying it would
    # invite a reader to think it did. The open/closed split is therefore reported
    # from the OLD queue's side, which is open-only by construction, rather than
    # from a column this pass does not consult.
    already = {row["canonical_id"] for row in await conn.fetch(OLD_QUEUE_SQL, user_id)}
    already_entering = [r for r in entering if r["canonical_id"] in already]
    new_exposure = [r for r in entering if r["canonical_id"] not in already]

    print("\n" + "=" * 72)
    status = "active" if active else "INACTIVE (waiting on a verified sender)"
    print(f"{email}   ({user_id})   {status}, onboarded={onboarded}")
    print("=" * 72)
    print(f"  filter: cities={sorted(prefs.effective_cities)} all_india={prefs.all_india} "
          f"keywords={len(prefs.keywords)} modes={sorted(prefs.work_modes)}")
    print(f"  entering the queue when the gate goes : {len(new_exposure):>6}")
    print(f"  already queued under the current gate : {len(already_entering):>6}   <-- the cost")
    print(f"  TOTAL to be recorded as already-seen  : {len(entering):>6}")

    if not entering:
        print("\n  Nothing. This account's filter admits no unrecorded row.")
        return []

    by_company: Counter[str] = Counter()
    for row in entering:
        by_company[companies.get(row["company_id"], f"company {row['company_id']}")] += 1

    print("\n  by company:")
    for name, n in by_company.most_common():
        print(f"    {name[:34]:<34} {n:>6}")

    print("\n  examples (the ones that would have been emailed as news):")
    for row in new_exposure[:15]:
        name = companies.get(row["company_id"], "?")
        print(f"    {name[:16]:<16} {(row['city'] or '?'):<14} {row['title'][:44]}")
    if len(new_exposure) > 15:
        print(f"    … and {len(new_exposure) - 15} more")

    return [row["canonical_id"] for row in entering]


async def main() -> int:
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    if not dry_run and "--apply" not in args:
        print(
            "Refusing to run without a mode.\n"
            "  --dry-run  report what would be recorded, write nothing\n"
            "  --apply    write it\n"
            "\nRun --dry-run first and read the per-account entering sets. This pass\n"
            "decides what several inboxes do NOT receive, and the only review that\n"
            "catches a wrong filter is a person reading the titles."
        )
        return 2

    conn = await get_crawler_conn()
    try:
        before = await fingerprint(conn)
        print(f"jobs lifecycle fingerprint before: {before[0]} ({before[1]} rows)")

        companies = {r["id"]: r["name"] for r in await conn.fetch(COMPANY_NAMES)}
        users = await conn.fetch(ACCOUNTS_SQL)
        inactive = sum(1 for u in users if not u["is_active"])
        print(
            f"{len(users)} account(s) with a filter ({inactive} inactive — see ACCOUNTS_SQL "
            f"for why those are IN scope), {len(companies)} companies in the catalog"
        )

        planned: list[tuple[object, str, list[str]]] = []
        for user in users:
            ids = await report_account(
                conn,
                user["id"],
                user["email"],
                user["is_active"],
                user["onboarding_completed"],
                companies,
            )
            planned.append((user["id"], user["email"], ids))

        total = sum(len(ids) for _, _, ids in planned)
        print("\n" + "=" * 72)
        print(f"TOTAL across {len(users)} account(s): {total} row(s) to record")
        print("=" * 72)

        if total == 0:
            print("\nNothing to do. Every active account's filter is already fully recorded.")
            report_fingerprint(before, await fingerprint(conn))
            return 0

        if dry_run:
            print("\n--dry-run: nothing written.")
            print("Read the per-account sections above. What you are approving is a list")
            print("of jobs that several people will now NEVER be emailed about — they stay")
            print("visible on every dashboard, which is the whole of what makes that")
            print("acceptable. Re-run with --apply when it looks right.")
            report_fingerprint(before, await fingerprint(conn))
            return 0

        # THE WRITE, THROUGH THE PRODUCTION FUNCTION. `before=None` says this
        # account has no prior filter to subtract from, so the delta is everything
        # the current one admits — which is exactly what the report above computed,
        # by the same call. A migration that reimplemented the rule would prove its
        # own reimplementation rather than the one the app runs.
        written = 0
        for user_id, email, ids in planned:
            if not ids:
                continue
            prefs = await load_preferences(conn, user_id)
            recorded = await suppress_filter_backlog(conn, user_id, None, prefs)
            written += len(recorded)
            print(f"  {email}: {len(recorded)} recorded")
            if len(recorded) != len(ids):
                print(
                    f"    NOTE: planned {len(ids)}, wrote {len(recorded)}. A crawl landing "
                    f"mid-run can add or close rows between the two reads; re-run to settle."
                )

        print(f"\nApplied: {written} row(s) recorded as already-seen.")
        clean = report_fingerprint(before, await fingerprint(conn))
        return 0 if clean else 1
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
