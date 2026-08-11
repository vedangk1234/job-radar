"""Establish jobs.salary_min / salary_max for the rows a description can answer for.

    python scripts/backfill_salary.py --dry-run   # report only, write nothing
    python scripts/backfill_salary.py --apply     # write, after you have read it

WHY THIS IS NEEDED AT ALL
-------------------------
Salary is written on the ENRICHMENT rail, beside work_mode and is_pm_role: a
description is read once, reduced to scalars, and thrown away (notify/
description.py explains why there is nowhere to put the text). So the rows read
BEFORE this slice have no salary and never will — the crawler does not re-read a
description it has already read successfully, which is the whole point of
desc_checked. On this database that is 87 of the 102 enrichable rows. Their
evidence is gone, so the only way to price them is to fetch again, once,
deliberately. That is this script.

WHAT MAKES THIS ONE DIFFERENT FROM backfill_work_mode.py
--------------------------------------------------------
It is expected to write NOTHING, and that is the honest prediction rather than a
hedge. Measured over a 36-row sample of this very candidate set, through this
very fetch path: 36 descriptions read cleanly, ZERO stated a number, 22 mentioned
salary/compensation/CTC without one. Indian postings on these boards do not
publish pay.

So the interesting output of this pass is not the distribution — it is the VETO
REPORT and the SAMPLE. A run that prices 0 rows and shows the guards catching
ESOP grants, funding rounds and application-form questions is the run working. A
run that prices 40 rows is a bug, and the sample is where that would be visible.

WHAT IT WRITES, AND WHAT IT REFUSES TO
--------------------------------------
Three columns: salary_min, salary_max, salary_checked. Every other column — the
five other description facts, and every lifecycle column — is absent from the
UPDATE, so this cannot re-alert a job, reopen a closed one, move a timestamp, or
claim a description was read when it was not. The lifecycle fingerprint below is
the evidence for that rather than the argument.

salary_checked IS THE POINT OF THE THIRD COLUMN AND OF THIS PARAGRAPH. It is set
TRUE on every row whose description was READ, including — especially — the rows
where no salary was found. Without it, "we read this posting and it states no
pay" and "nobody has looked at this row" would both be salary_min IS NULL, which
is jobs.notified with a decimal point: one column carrying two facts, told apart
by nothing. With it, this pass is idempotent in COST as well as in effect — a
second run skips the 102 rows it already read instead of re-fetching them.

IT DOES NOT MARK ANYTHING desc_checked, deliberately. A row that was unread stays
unread, so the ordinary crawl still owes it a full read of all its facts when it
resumes. Since that read runs the same parser over the same description, it
writes the same salary this pass did — which is the property that makes running
this against a paused crawler safe.
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.connection import get_crawler_conn  # noqa: E402
from notify.description import company_of, explain_salary, read_description  # noqa: E402
from notify.match import Preferences, is_match, salary_ok, worth_a_description  # noqa: E402
from notify.store import load_active_preferences  # noqa: E402

# Every open row, with the columns both the seam and the matcher read. Joined to
# companies because fetching a description needs the vendor, the token and the
# config — that is the company's knowledge, not the job's.
LOAD = """
SELECT j.canonical_id, j.title, j.location, j.city, j.url, j.work_mode,
       j.salary_min, j.salary_max, j.salary_checked,
       j.min_experience, j.is_pm_role, j.desc_checked, j.desc_fetch_failed,
       c.name AS company, c.ats, c.token, c.config
  FROM jobs j
  JOIN companies c ON c.id = j.company_id
 WHERE j.status = 'open'
 ORDER BY c.name, j.title
"""

# The WHOLE filter, not just the bands: the blast radius is computed with
# notify.match.is_match, and handing that a partial row would make it judge
# against defaults nobody chose — a report about a filter that does not exist.
# The same eleven columns notify/store.PREFERENCES_SQL selects, plus the two this
# report prints.
PREFS = """
SELECT u.email, u.is_active,
       p.cities, p.selected_cities, p.all_india,
       p.keywords, p.exclusions, p.max_level,
       p.exp_range_low, p.exp_range_high, p.show_no_experience_jobs,
       p.work_modes, p.salary_bands
  FROM preferences p
  JOIN public.users u ON u.id = p.user_id
 ORDER BY u.email
"""

# Three columns, named once. `IS DISTINCT FROM` on all three so a re-run writes
# nothing — including for the overwhelmingly common row where the verdict is "no
# pay stated", whose salary_checked flips FALSE -> TRUE exactly once, ever.
UPDATE = """
UPDATE jobs
   SET salary_min     = $2,
       salary_max     = $3,
       salary_checked = $4
 WHERE canonical_id = $1
   AND (salary_min     IS DISTINCT FROM $2
     OR salary_max     IS DISTINCT FROM $3
     OR salary_checked IS DISTINCT FROM $4)
RETURNING canonical_id
"""

# Every column of jobs EXCEPT the three this pass writes, hashed per row before
# and after. Same instrument scripts/relabel_cities.py and backfill_work_mode.py
# use, and for the same reason: "the UPDATE only names these three" is an
# argument, and this is evidence.
FINGERPRINT = """
SELECT canonical_id,
       md5(
         concat_ws('|',
           company_id::text, title, location, city, url,
           source_posted_at::text, first_seen_at::text, last_seen_at::text,
           miss_count::text, status, seeded::text,
           min_experience::text, is_pm_role::text, desc_checked::text,
           desc_fetch_failed::text, desc_checked_at::text,
           applied::text, applied_at::text, work_mode
         )
       ) AS h
  FROM jobs
 ORDER BY canonical_id
"""

LIFECYCLE_COLUMNS = (
    "company_id", "title", "location", "city", "url", "source_posted_at",
    "first_seen_at", "last_seen_at", "miss_count", "status", "seeded",
    "min_experience", "is_pm_role", "desc_checked", "desc_fetch_failed",
    "desc_checked_at", "applied", "applied_at", "work_mode",
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
    print("LIFECYCLE FINGERPRINT  (every column except the three this pass writes)")
    print("-" * 78)
    print(f"  covering: {', '.join(LIFECYCLE_COLUMNS)}")
    print(f"  before:   {before_hash}  ({len(before_rows)} rows)")
    print(f"  after:    {after_hash}  ({len(after_rows)} rows)")
    if before_hash == after_hash:
        print("  MATCH — nothing outside salary_min/salary_max/salary_checked changed.")
        return True
    print("  MISMATCH — something else moved. Details:")
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

    THE VENDOR'S FIGURE WINS OVER THE PROSE, exactly as it does in
    enrich_candidates — the same precedence, resolved the same way, so this pass
    and the crawl cannot disagree about a row an ATS prices itself.
    """
    out: dict[str, dict] = {}
    for index, job in enumerate(candidates, 1):
        read = await read_description(company_of(job), job)
        parsed = explain_salary(read.text)

        if read.salary_min is not None:
            low, high, marker, snippet = (
                read.salary_min, read.salary_max, f"{job['ats']} field", None,
            )
            from_ats = True
        else:
            low, high, marker, snippet = (
                parsed.low, parsed.high, parsed.marker, parsed.snippet,
            )
            from_ats = False

        out[job["canonical_id"]] = {
            "low": low,
            "high": high,
            "marker": marker,
            "snippet": snippet,
            "vetoes": parsed.vetoes,
            "from_ats": from_ats,
            "fetched": read.text is not None,
        }
        if index % 20 == 0:
            print(f"    ... {index}/{len(candidates)}")
    return out


def report_distribution(verdicts, candidates) -> None:
    print("\n" + "-" * 78)
    print("PROJECTED SALARY  (what this pass would establish)")
    print("-" * 78)
    priced = [v for v in verdicts.values() if v["low"] is not None]
    read_ok = [v for v in verdicts.values() if v["fetched"]]
    print(f"    {'candidates':<34} {len(candidates):>6}")
    print(f"    {'descriptions read':<34} {len(read_ok):>6}")
    print(f"    {'fetch failed (stay unchecked)':<34} {len(verdicts) - len(read_ok):>6}")
    print(f"    {'PRICED (salary established)':<34} {len(priced):>6}")
    print(f"    {'read, but state no pay':<34} {len(read_ok) - len(priced):>6}")
    if priced:
        by_ats = sum(1 for v in priced if v["from_ats"])
        print(f"\n    of the priced, stated by the ATS itself: {by_ats}")
        print(f"    the rest were read out of the description prose.")

    print("\n    BAND COVERAGE of the priced rows (against all six bands):")
    if not priced:
        print("      none — no row would carry a salary, so no band is exercised.")
    else:
        for band in ("<10", "10-15", "15-25", "25-40", "40-50", "50+"):
            prefs = Preferences(salary_bands=(band,))
            n = sum(
                1 for v in priced
                if salary_ok({"salary_min": v["low"], "salary_max": v["high"]}, prefs)
            )
            print(f"      {band:<8} {n:>4}")

    print("\n    EVERY ROW NOT LISTED ABOVE KEEPS salary_min NULL AND salary_checked FALSE.")
    print("    An unpriced job is shown to everyone whatever they ticked, so a NULL here")
    print("    costs nobody a posting — see notify.match.salary_ok.")


def report_sample(verdicts) -> None:
    """THE LIST TO EYEBALL: every salary this pass would write, with its evidence.

    A person cannot audit a hundred numbers, but they can audit the sentences the
    numbers came out of — the same instrument scripts/relabel_cities.py's residue
    report and the work-mode marker report use.

    A false positive here looks like a plausible rupee figure attached to
    something that is not a wage: an ESOP grant, an insurance cover, a funding
    round. The snippet is what makes that visible, and a regex cannot.
    """
    print("\n" + "-" * 78)
    print("SAMPLE  (every parsed salary, with the text that produced it)")
    print("-" * 78)
    priced = [(cid, v) for cid, v in verdicts.items() if v["low"] is not None]
    if not priced:
        print("  NONE — no description in this candidate set states a salary.")
        print("  That is the expected result on this board and is not a failure:")
        print("  36 of 36 sampled live descriptions stated no number before this was built.")
        return
    for cid, v in priced:
        span = f"{v['low']} LPA" if v["low"] == v["high"] else f"{v['low']}-{v['high']} LPA"
        print(f"\n  {cid}")
        print(f"      salary   : {span}   via {v['marker']}")
        if v["snippet"]:
            print(f"      evidence : {v['snippet']}")


def report_vetoes(verdicts) -> None:
    """Which guards fired, and how often. The false positives that were CAUGHT.

    This is the half of the report that has something to show on today's board,
    and it is the reviewable evidence that the parser's silence is deliberate
    rather than accidental — a parser that found nothing because its regexes were
    broken would show zero vetoes here too, and these two states must be
    distinguishable.
    """
    print("\n" + "-" * 78)
    print("VETO REPORT  (money in these descriptions that is NOT this job's pay)")
    print("-" * 78)
    fired = Counter()
    for v in verdicts.values():
        for name in v["vetoes"]:
            fired[name] += 1
    if not fired:
        print("  NONE fired. Every description was free of currency figures entirely.")
        return
    print(f"    {'guard':<32} {'rows':>6}")
    for name, n in fired.most_common():
        print(f"    {name:<32} {n:>6}")
    rows_with = sum(1 for v in verdicts.values() if v["vetoes"])
    print(f"\n    {rows_with} of {len(verdicts)} descriptions contained at least one")
    print("    figure a guard had to stop. Those are the rows a parser without these")
    print("    guards would have priced wrongly.")


def report_blast_radius(prefs_pairs, rows, verdicts) -> None:
    """Per account: what enters and what leaves, judged by the WHOLE matcher.

    Not just the pay gate. The verdict is applied to the row and the entire
    is_match is re-run, so a report line of 0/0 means this pass changes nobody's
    digest — which is the expected and required outcome, because the operator
    ticked all six bands and an unpriced job passes the gate regardless.
    """
    print("\n" + "-" * 78)
    print("BLAST RADIUS  (per account, full is_match before vs after)")
    print("-" * 78)
    for pref_row, prefs_obj in prefs_pairs:
        before = {r["canonical_id"] for r in rows if is_match(r, prefs_obj)}
        after_rows = [
            {
                **r,
                "salary_min": verdicts.get(r["canonical_id"], {}).get("low", r["salary_min"]),
                "salary_max": verdicts.get(r["canonical_id"], {}).get("high", r["salary_max"]),
            }
            for r in rows
        ]
        after = {r["canonical_id"] for r in after_rows if is_match(r, prefs_obj)}

        entering = after - before
        leaving = before - after
        active = "" if pref_row["is_active"] else "   (is_active FALSE — not in the fan-out)"
        print(f"\n  {pref_row['email']}{active}")
        print(f"      salary_bands   = {list(pref_row['salary_bands'])}")
        print(f"      matches before : {len(before)}")
        print(f"      matches after  : {len(after)}")
        print(f"      entering       : {len(entering)}")
        print(f"      leaving        : {len(leaving)}")
        for cid in list(leaving)[:20]:
            row = next(r for r in rows if r["canonical_id"] == cid)
            v = verdicts.get(cid, {})
            print(f"        - {row['company'][:16]:<16} {row['title'][:40]}"
                  f"   -> {v.get('low')}-{v.get('high')} LPA")


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
        # the function itself, imported, so the set this pass reads and the set
        # the next crawl reads cannot drift apart.
        #
        # Gated on salary_checked rather than on `salary_min IS NULL`, which is
        # what makes a second run cheap: NULL is the answer for almost every row
        # that HAS been read, so a NULL-gated pass would re-fetch all 102 every
        # time it ran.
        candidates = [
            r for r in rows
            if any(worth_a_description(r, prefs) for prefs in filters)
            and not r["salary_checked"]
        ]
        print(f"candidates (worth_a_description, salary_checked FALSE): {len(candidates)}")
        print(f"  never read before  : {sum(1 for r in candidates if not r['desc_checked'])}")
        print(f"  read before this slice existed: {sum(1 for r in candidates if r['desc_checked'])}")

        if not candidates:
            print("\nNothing to establish.")
            return 0

        print(f"\nfetching {len(candidates)} description(s) through the ordinary polite path…")
        verdicts = await read_all(candidates)

        report_distribution(verdicts, candidates)
        report_vetoes(verdicts)
        report_sample(verdicts)

        prefs_pairs = [(p, Preferences.from_row(p)) for p in pref_rows]
        report_blast_radius(prefs_pairs, rows, verdicts)

        # EVERY ROW WHOSE DESCRIPTION WAS READ IS WRITABLE, not only the priced
        # ones — that is the salary_checked column doing its job. A row that was
        # read and states no pay gets (NULL, NULL, TRUE), which is a genuine
        # change from (NULL, NULL, FALSE) and the whole reason the distinction
        # exists.
        writable = [
            (cid, v["low"], v["high"], True)
            for cid, v in verdicts.items()
            if v["fetched"]
        ]
        priced = sum(1 for _, low, _, _ in writable if low is not None)
        print("\n" + "-" * 78)
        print(f"WOULD MARK {len(writable)} row(s) salary_checked; {priced} of them PRICED.")
        print(f"{len(verdicts) - len(writable)} row(s) could not be fetched and stay unchecked.")

        # THE FINGERPRINT WINDOW IS THE WRITE, NOT THE RUN. Bracketing the whole
        # pass would bracket the minutes of description fetching too, and the
        # hourly crawl lands inside that window: rows appear, last_seen_at moves,
        # and the gate reports a MISMATCH about somebody else's work. Observed on
        # the first live run of backfill_work_mode.py, which is how this window
        # got its shape.
        before_fp = await fingerprint(conn)
        print(f"\nlifecycle fingerprint (immediately before the write): {before_fp[0]}")

        if not apply:
            fp_clean = report_fingerprint(before_fp, await fingerprint(conn))
            print("\n" + ("DRY RUN CLEAN — nothing was written." if fp_clean
                          else "DRY RUN WROTE SOMETHING. Do not apply."))
            print("\nRead the SAMPLE and the VETO REPORT above. Re-run with --apply to write.")
            return 0 if fp_clean else 1

        written = 0
        async with conn.transaction():
            for start in range(0, len(writable), CHUNK):
                for cid, low, high, checked in writable[start : start + CHUNK]:
                    if await conn.fetchval(UPDATE, cid, low, high, checked):
                        written += 1
        print(f"\nupdated {written} row(s).")

        [totals] = await conn.fetch(
            "SELECT count(*) FILTER (WHERE salary_checked) AS checked, "
            "       count(*) FILTER (WHERE salary_min IS NOT NULL) AS priced, "
            "       count(*) AS total FROM jobs"
        )
        print(f"jobs now: {totals['checked']} salary_checked, "
              f"{totals['priced']} priced, {totals['total']} total")

        clean = report_fingerprint(before_fp, await fingerprint(conn))
        return 0 if clean else 1
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
