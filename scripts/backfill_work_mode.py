"""Establish jobs.work_mode for the rows a description read can answer for.

    python scripts/backfill_work_mode.py --dry-run   # report only, write nothing
    python scripts/backfill_work_mode.py --apply     # write, after you have read it

WHY THIS IS NEEDED AT ALL
-------------------------
work_mode is written on the ENRICHMENT rail, beside is_pm_role: a description is
read once, reduced to scalars, and thrown away (notify/description.py explains
why there is nowhere to put the text). Two consequences follow, and this script
exists for both.

The rows read BEFORE this slice have no work_mode and never will — the crawler
will not re-read a description it has already read successfully, which is the
whole point of desc_checked. Their evidence is gone, so the only way to establish
their arrangement is to fetch again, once, deliberately. That is this script.

And the rows the WIDENED seam newly covers have never been read at all. Cities
Slice B built a rule that exempts a remote posting from the city gate, and it
could not fire: the enrichment queue was narrowed by title_stage_ok, which
INCLUDES city_ok, so a job that the relaxation would rescue was exactly a job
that never earned a description read. notify.match.worth_a_description is the fix
and this pass is what applies it to the board as it already stands.

WHAT IT WRITES, AND WHAT IT REFUSES TO
--------------------------------------
Only `work_mode`, and only where a read produced a verdict. Every other column —
the four other description facts, and every lifecycle column — is absent from the
UPDATE, so this cannot re-alert a job, reopen a closed one, move a timestamp, or
claim a description was read when it was not. The lifecycle fingerprint below is
the evidence for that rather than the argument.

NULL IS NEVER WRITTEN OVER AND NEVER WRITTEN IN. A row whose description could
not be fetched keeps NULL, and NULL means "nobody has established this posting's
arrangement". notify.match.work_mode_of reads that as onsite for the purpose of
FILTERING and the column keeps saying nothing, so "we read it and it said
nothing" stays distinguishable from "we never looked". Collapsing those two into
a stored 'onsite' would be jobs.notified again — one column, two facts — and that
took four sub-phases to undo.

IT DOES NOT MARK ANYTHING AS CHECKED, deliberately. A row that was unread stays
unread, so the ordinary crawl still owes it a full read of all five facts when it
resumes. Since that read runs the same parser over the same description, it
writes the same work_mode this pass did — which is the property that makes
running this against a paused crawler safe.

IDEMPOTENT. The verdict is a pure function of the description and of the vendor's
own field, and rows already carrying the right value are skipped by the
`IS DISTINCT FROM` comparison rather than rewritten.
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.connection import get_crawler_conn  # noqa: E402
from notify.description import (  # noqa: E402
    company_of,
    detect_work_mode,
    explain_work_mode,
    read_description,
)
from notify.match import city_ok, is_match, work_mode_of, worth_a_description  # noqa: E402
from notify.store import load_active_preferences  # noqa: E402

# Every open row, with the columns both the seam and the matcher read. Joined to
# companies because fetching a description needs the vendor, the token and the
# config — that is the company's knowledge, not the job's.
LOAD = """
SELECT j.canonical_id, j.title, j.location, j.city, j.url, j.work_mode,
       j.min_experience, j.is_pm_role, j.desc_checked, j.desc_fetch_failed,
       c.name AS company, c.ats, c.token, c.config
  FROM jobs j
  JOIN companies c ON c.id = j.company_id
 WHERE j.status = 'open'
 ORDER BY c.name, j.title
"""

# The WHOLE filter, not just the city and mode columns: the blast radius is
# computed with notify.match.is_match, and handing that a partial row would make
# it judge against defaults nobody chose — a report about a filter that does not
# exist. The same ten columns notify/store.PREFERENCES_SQL selects, plus the two
# this report prints.
PREFS = """
SELECT u.email, u.is_active,
       p.cities, p.selected_cities, p.all_india,
       p.keywords, p.exclusions, p.max_level,
       p.exp_range_low, p.exp_range_high, p.show_no_experience_jobs,
       p.work_modes
  FROM preferences p
  JOIN public.users u ON u.id = p.user_id
 ORDER BY u.email
"""

# One column, named once. `IS DISTINCT FROM` so a re-run writes nothing.
UPDATE = """
UPDATE jobs
   SET work_mode = $2
 WHERE canonical_id = $1
   AND work_mode IS DISTINCT FROM $2
RETURNING canonical_id
"""

# Every column of jobs EXCEPT work_mode, hashed per row before and after. Same
# instrument scripts/relabel_cities.py uses and for the same reason: "the UPDATE
# only names work_mode" is an argument, and this is evidence.
FINGERPRINT = """
SELECT canonical_id,
       md5(
         concat_ws('|',
           company_id::text, title, location, city, url,
           source_posted_at::text, first_seen_at::text, last_seen_at::text,
           miss_count::text, status, seeded::text,
           min_experience::text, is_pm_role::text, desc_checked::text,
           desc_fetch_failed::text, desc_checked_at::text,
           applied::text, applied_at::text
         )
       ) AS h
  FROM jobs
 ORDER BY canonical_id
"""

LIFECYCLE_COLUMNS = (
    "company_id", "title", "location", "city", "url", "source_posted_at",
    "first_seen_at", "last_seen_at", "miss_count", "status", "seeded",
    "min_experience", "is_pm_role", "desc_checked", "desc_fetch_failed",
    "desc_checked_at", "applied", "applied_at",
)

CHUNK = 200


async def fingerprint(conn) -> tuple[str, dict[str, str]]:
    rows = await conn.fetch(FINGERPRINT)
    per_row = {r["canonical_id"]: r["h"] for r in rows}
    rollup = hashlib.md5(
        "".join(f"{cid}:{h}" for cid, h in sorted(per_row.items())).encode("utf-8")
    ).hexdigest()
    return rollup, per_row


def report_fingerprint(before, after) -> bool:
    (before_hash, before_rows), (after_hash, after_rows) = before, after
    print("\n" + "-" * 78)
    print("LIFECYCLE FINGERPRINT  (every column except `work_mode`)")
    print("-" * 78)
    print(f"  covering: {', '.join(LIFECYCLE_COLUMNS)}")
    print(f"  before:   {before_hash}  ({len(before_rows)} rows)")
    print(f"  after:    {after_hash}  ({len(after_rows)} rows)")
    if before_hash == after_hash:
        print("  MATCH — no column other than `work_mode` changed, and no row moved.")
        return True
    print("  MISMATCH — something other than `work_mode` moved. Details:")
    changed = [c for c in sorted(set(before_rows) & set(after_rows))
               if before_rows[c] != after_rows[c]]
    for cid in changed[:20]:
        print(f"    row altered: {cid}")
    if len(changed) > 20:
        print(f"    ... and {len(changed) - 20} more")
    return False


async def read_all(candidates) -> dict[str, dict]:
    """Fetch every candidate's description ONCE and reduce it to a verdict.

    Through notify.description.read_description — the production path, the same
    polite client, the same per-host pacing, the same Workday slowdown. Nothing
    here has its own idea of how to talk to a board, which is what makes "the
    parser writes what the crawler will write" true rather than hoped for.

    Sequential, like enrich_candidates and for its reason: the set is small, the
    shared client already paces per host, and one request at a time is the
    version whose behaviour against somebody else's board is obvious.
    """
    out: dict[str, dict] = {}
    for index, job in enumerate(candidates, 1):
        read = await read_description(company_of(job), job)
        stated = read.work_mode
        mode, marker, snippet = explain_work_mode(job.get("title"), read.text)
        if stated:
            mode, marker, snippet = stated, f"{job['ats']} field", None
        out[job["canonical_id"]] = {
            "mode": mode,
            "marker": marker,
            "snippet": snippet,
            "from_ats": stated is not None,
            "fetched": read.text is not None,
        }
        if index % 20 == 0:
            print(f"    ... {index}/{len(candidates)}")
    return out


def report_distribution(verdicts, candidates) -> None:
    print("\n" + "-" * 78)
    print("PROJECTED work_mode  (stored now -> after this pass)")
    print("-" * 78)
    spread = Counter(v["mode"] if v["mode"] else "NULL (unreadable)" for v in verdicts.values())
    print(f"    {'verdict':<24} {'rows':>7}")
    for label, n in spread.most_common():
        print(f"    {label:<24} {n:>7}")
    print(f"    {'TOTAL candidates':<24} {len(candidates):>7}")
    by_ats = Counter(v["mode"] for v in verdicts.values() if v["from_ats"])
    print(f"\n    of those, stated by the ATS itself: {sum(by_ats.values())} {dict(by_ats)}")
    print(f"    the rest were read out of the description prose.")
    print("\n    EVERY ROW NOT LISTED ABOVE KEEPS work_mode NULL — around 9,600 of them.")
    print("    NULL is read as 'onsite' when filtering and is never written down.")


def report_markers(verdicts) -> None:
    """Every non-onsite verdict, grouped by the phrase that produced it.

    THE REVIEWABLE UNIT IS THE MARKER, NOT THE ROW. A person cannot audit a
    hundred verdicts one at a time, but they can audit the dozen phrases those
    verdicts came from — and a wrong phrase is what a wrong verdict is made of.
    Same instrument as scripts/relabel_cities.py's residue report, pointed at a
    detector instead of at a marker table.
    """
    print("\n" + "-" * 78)
    print("MARKER REPORT  (what made a row anything other than onsite)")
    print("-" * 78)
    interesting = [(cid, v) for cid, v in verdicts.items() if v["mode"] in ("remote", "hybrid")]
    if not interesting:
        print("  NONE — every readable description stated no arrangement, or stated onsite.")
        return
    by_marker: dict[tuple[str, str], int] = Counter(
        (v["mode"], v["marker"] or "(none)") for _, v in interesting
    )
    for (mode, marker), n in by_marker.most_common():
        print(f"    {mode:<8} {n:>4}   {marker}")


def report_added(added, verdicts) -> None:
    """THE LIST TO EYEBALL: every job the remote relaxation newly admits.

    A job is here iff it failed the city gate before and passes it now BECAUSE
    the parser called it remote. That is the one direction this slice can add to
    an inbox, and each row carries the evidence that produced it — the location
    field as published, the verdict, and the sentence the detector matched.

    A false positive here looks like a foreign or office job with a plausible
    sentence behind it, which is exactly what a person can spot and a regex
    cannot.
    """
    print("\n" + "-" * 78)
    print("ADDED BY remote-relaxes-city  (jobs that would newly reach a digest)")
    print("-" * 78)
    if not added:
        print("  NONE.")
        return
    for email, rows in added.items():
        print(f"\n  {email}:  {len(rows)} job(s)")
        for job in rows:
            v = verdicts[job["canonical_id"]]
            print(f"\n    + {job['company']} — {job['title']}")
            print(f"      location   : {job['location']!r}   (stored city: {job['city']})")
            print(f"      work_mode  : {v['mode']}   via {v['marker']}")
            if v["snippet"]:
                print(f"      evidence   : {v['snippet']}")


def report_blast_radius(prefs, rows, verdicts) -> dict:
    """Per account: what enters and what leaves, judged by the WHOLE matcher.

    Not just the city gate. work_mode has two effects — it can relax the city
    test and it can fail the arrangement test — so a report that looked only at
    the first would miss every job the second removes.
    """
    print("\n" + "-" * 78)
    print("BLAST RADIUS  (per account, full is_match before vs after)")
    print("-" * 78)
    added_by_user: dict[str, list] = {}
    for pref_row, prefs_obj in prefs:
        before = {r["canonical_id"] for r in rows if is_match(r, prefs_obj)}
        after_rows = [
            {**r, "work_mode": verdicts.get(r["canonical_id"], {}).get("mode") or r["work_mode"]}
            for r in rows
        ]
        after = {r["canonical_id"] for r in after_rows if is_match(r, prefs_obj)}

        entering = [r for r in after_rows if r["canonical_id"] in after - before]
        leaving = [r for r in rows if r["canonical_id"] in before - after]

        # Of the arrivals, which are here because the CITY gate relaxed?
        by_remote = [
            r for r in entering
            if not city_ok({**r, "work_mode": None}, prefs_obj) and city_ok(r, prefs_obj)
        ]
        added_by_user[pref_row["email"]] = by_remote

        active = "" if pref_row["is_active"] else "   (is_active FALSE — not in the fan-out)"
        print(f"\n  {pref_row['email']}{active}")
        print(f"      work_modes = {list(pref_row['work_modes'])}")
        print(f"      matches before : {len(before)}")
        print(f"      matches after  : {len(after)}")
        print(f"      entering       : {len(entering)}   ({len(by_remote)} via remote-relaxes-city)")
        print(f"      leaving        : {len(leaving)}")
        for r in leaving[:20]:
            print(f"        - {r['company'][:16]:<16} {r['title'][:44]}"
                  f"   -> {work_mode_of({**r, 'work_mode': verdicts.get(r['canonical_id'], {}).get('mode')})}")
    return added_by_user


async def main() -> int:
    apply = "--apply" in sys.argv[1:]
    if not apply and "--dry-run" not in sys.argv[1:]:
        print("pass --dry-run to report, or --apply to write. Refusing to guess.")
        return 2

    conn = await get_crawler_conn()
    try:
        rows = [dict(r) for r in await conn.fetch(LOAD)]
        pref_rows = await conn.fetch(PREFS)
        filters = await load_active_preferences(conn)
        print(f"open rows: {len(rows)}   active filters: {len(filters)}")

        # THE SAME SEAM THE CRAWLER WILL USE ON RESUME. Not a re-statement of it —
        # the function itself, imported, so the set this pass reads and the set the
        # next crawl reads cannot drift apart.
        candidates = [
            r for r in rows
            if any(worth_a_description(r, prefs) for prefs in filters)
            and r["work_mode"] is None
        ]
        print(f"candidates (worth_a_description, work_mode still NULL): {len(candidates)}")
        print(f"  never read before  : {sum(1 for r in candidates if not r['desc_checked'])}")
        print(f"  read before this slice existed: {sum(1 for r in candidates if r['desc_checked'])}")

        if not candidates:
            print("\nNothing to establish.")
            return 0

        print(f"\nfetching {len(candidates)} description(s) through the ordinary polite path…")
        verdicts = await read_all(candidates)

        report_distribution(verdicts, candidates)
        report_markers(verdicts)

        # Preferences objects, paired with the row they came from for the report.
        from notify.match import Preferences

        prefs_pairs = [(p, Preferences.from_row(p)) for p in pref_rows]
        added = report_blast_radius(prefs_pairs, rows, verdicts)
        report_added(added, verdicts)

        writable = [
            (cid, v["mode"]) for cid, v in verdicts.items() if v["mode"] is not None
        ]
        print("\n" + "-" * 78)
        print(f"WOULD WRITE work_mode on {len(writable)} row(s); "
              f"{len(verdicts) - len(writable)} stay NULL (unreadable).")

        # THE FINGERPRINT IS TAKEN HERE, NOT AT THE TOP, AND THE WINDOW IS THE
        # POINT. What this gate has to prove is "the UPDATE touched only
        # work_mode" — a claim about the write. Bracketing the whole run instead
        # would bracket the three minutes of description fetching too, and the
        # hourly crawl lands inside that window: rows appear, last_seen_at moves,
        # and the gate reports a MISMATCH about somebody else's work. Observed on
        # the first live run of this script, which is how the window got fixed.
        #
        # scripts/relabel_cities.py brackets its whole run and gets away with it
        # because that pass computes locally and finishes in seconds. This one
        # makes 100+ network requests, so it cannot.
        before_fp = await fingerprint(conn)
        print(f"\nlifecycle fingerprint (immediately before the write): {before_fp[0]}")

        if not apply:
            fp_clean = report_fingerprint(before_fp, await fingerprint(conn))
            print("\n" + ("DRY RUN CLEAN — nothing was written." if fp_clean
                          else "DRY RUN WROTE SOMETHING. Do not apply."))
            print("\nRead the ADDED list above. Re-run with --apply to write.")
            return 0 if fp_clean else 1

        written = 0
        async with conn.transaction():
            for start in range(0, len(writable), CHUNK):
                for cid, mode in writable[start : start + CHUNK]:
                    if await conn.fetchval(UPDATE, cid, mode):
                        written += 1
        print(f"\nupdated {written} row(s).")

        spread = await conn.fetch(
            "SELECT work_mode, count(*) n FROM jobs GROUP BY work_mode ORDER BY n DESC"
        )
        print("work_mode now: " + ", ".join(f"{r['work_mode']}={r['n']}" for r in spread))

        clean = report_fingerprint(before_fp, await fingerprint(conn))
        return 0 if clean else 1
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
