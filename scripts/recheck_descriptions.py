"""Re-open the description reads a detector change has invalidated.

    python scripts/recheck_descriptions.py --dry-run   # report only, write nothing
    python scripts/recheck_descriptions.py             # clear the flag, then crawl

WHY THIS EXISTS
---------------
It is scripts/relabel_cities.py's argument, applied to the other pair of
read-once columns. `city` is computed at crawl time from the location string and
stored; `min_experience` and `is_pm_role` are computed once from the description
and stored, and the description itself is deliberately thrown away
(notify/description.py explains why there is nowhere to put it). Both are the
same contract — the evidence is not the stored thing, the conclusion is — and
both have the same consequence: improving the code that draws the conclusion
does not reach the rows already holding one.

The city version can just recompute, because the evidence is still in the row.
This one cannot. The only way to re-decide is to fetch the description again, so
this pass does not compute anything at all — it clears `desc_checked`, which is
the flag crawler/enrich.py reads to decide what is worth a request. The next
crawl does the actual work, through the ordinary path, with the ordinary
politeness. Nothing here fetches anything.

WHICH ROWS, AND WHY NOT MORE
----------------------------
Only rows whose stored conclusion is one the change can overturn:

    status = 'open'              a closed posting is not worth a request
    desc_checked = TRUE          an unread row is already in the queue
    desc_fetch_failed = FALSE    a failed row already retries on its own timer
    min_experience IS NULL       "read it, found no minimum" — the one verdict
                                 a better parser can turn into a number

The last clause is what keeps this honest rather than a `desc_checked = FALSE`
over the whole table. A row that already carries a number was read successfully
by a parser that found something, and detect_min_experience returns the SMALLEST
confident figure it sees — so a new anchor can only lower that number or leave
it alone, and lowering it can only ever keep a job that was hidden. There is no
version of that worth a second request to nobody's board.

...and then the same seam crawler/enrich.py uses, applied HERE rather than left
to it: the SQL above cannot express `title_stage_ok`, so the set is narrowed in
Python before anything is written. Clearing a row the title filter rejects would
be free but not harmless — enrichment never asks about such a row, so the flag
would sit at FALSE forever, and "nobody has read this description" would be
recorded about a description that was read. `desc_checked` is a fact, and a pass
that invalidates it should invalidate only the ones it can arrange to re-derive.

NOT IDEMPOTENT, AND THAT IS THE ONE THING TO KNOW BEFORE RUNNING IT
-------------------------------------------------------------------
scripts/relabel_cities.py converges: run it twice and the second run writes
nothing, because the label it would write is already there. This one cannot,
and the difference is the discarded text. Most descriptions state no minimum at
all, so a row re-read successfully comes back to `min_experience IS NULL` — the
very state this pass selects on. Run it again and it selects the same rows and
buys the same answer at the price of the same requests.

So it is a MANUAL step taken deliberately, once, after a detector in
notify/description.py has actually changed. It is not on the crawl path, not in
the cron, and running it on a schedule would be a slow re-download of the same
descriptions forever. Check `git log -p notify/description.py` before reaching
for it: if nothing changed, there is nothing here to re-derive.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.connection import get_crawler_conn  # noqa: E402
from notify.match import title_stage_ok  # noqa: E402
from notify.store import load_preferences  # noqa: E402

# The candidate set, joined to companies so the report can name the employer and
# the caller can see which boards the next crawl will actually ask.
LOAD = """
SELECT j.canonical_id, j.title, j.city, j.location, j.min_experience,
       j.is_pm_role, j.desc_checked, j.desc_fetch_failed, j.desc_checked_at,
       c.name AS company, c.ats, c.tracked
  FROM jobs j
  JOIN companies c ON c.id = j.company_id
 WHERE j.status = 'open'
   AND j.desc_checked = TRUE
   AND j.desc_fetch_failed = FALSE
   AND j.min_experience IS NULL
 ORDER BY c.name, j.title
"""

# One column, and deliberately not desc_checked_at: that timestamp records when
# we last LOOKED, and this pass does not look. Leaving it alone keeps it an
# honest answer to "when did a fetch last happen for this row".
#
# There is no lifecycle column here and there must not be — the same rule
# notify/store.SAVE_DESCRIPTION_FACTS_SQL follows, for the same reason. Clearing
# a read must not be able to re-open a job or un-send one.
CLEAR = """
UPDATE jobs
   SET desc_checked = FALSE
 WHERE canonical_id = $1
   AND status = 'open'
   AND desc_checked = TRUE
   AND desc_fetch_failed = FALSE
   AND min_experience IS NULL
RETURNING canonical_id
"""

CHUNK = 200


async def main() -> int:
    dry_run = "--dry-run" in sys.argv[1:]

    conn = await get_crawler_conn()
    try:
        prefs = await load_preferences(conn)
        rows = [dict(r) for r in await conn.fetch(LOAD)]

        # The title filter is the whole selection, not a preview of it: a row it
        # rejects is one enrichment will never ask about, so clearing it would
        # strand the flag at FALSE rather than schedule a re-read.
        targets = [r for r in rows if title_stage_ok(r, prefs)]

        print(f"open rows read cleanly with no minimum found: {len(rows)}")
        print(f"  of those, title-stage matches (= what this clears): {len(targets)}")
        print(f"  left alone (the title filter already excludes them): {len(rows) - len(targets)}")

        if targets:
            print("\n  the next crawl will re-read:")
            for r in targets:
                print(f"    {r['company'][:18]:<18} {r['ats']:<16} {str(r['city']):<14} "
                      f"{(r['title'] or '')[:52]}")

        if not targets:
            print("\nNothing to clear.")
            return 0

        if dry_run:
            print(f"\n--dry-run: {len(targets)} row(s) left untouched.")
            return 0

        cleared = 0
        async with conn.transaction():
            for start in range(0, len(targets), CHUNK):
                for r in targets[start : start + CHUNK]:
                    if await conn.fetchval(CLEAR, r["canonical_id"]):
                        cleared += 1

        print(f"\ncleared desc_checked on {cleared} row(s).")
        print("Run `python scripts/crawl.py` (or wait for the hourly cron) to re-read them.")
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
