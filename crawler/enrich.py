"""The crawl's second stage: read the descriptions of the jobs that fit.

    fetch -> diff -> store        (crawler/loop.py, once per company)
    -> ENRICH                     (this module, once per crawl)

Why this is part of a crawl at all
----------------------------------
It was not, and that was the bug. Enrichment used to live only in the digest
path, which reads one person's send queue — so a description was read the first
time a job was considered for an EMAIL. The dashboard's Home screen is a
different question: it shows everything currently open that fits, already
emailed or not. A job the crawler discovered an hour ago was therefore on Home
with desc_checked = FALSE, nothing known about its experience requirement, and
kept — because unknown means keep, everywhere in this project, on purpose.

That is correct behaviour on incomplete evidence, and the fix is the evidence
rather than the rule. A real Adobe posting, "Computer Scientist - Fullstack",
asks for 4-6 years in a line no one had ever fetched. It passed every title test
(Bengaluru, "full stack", no excluded word, no level notation) and sat on Home
looking like a job worth applying to.

So the description is read during the crawl, and one crawl cycle is now the
worst case for how long a job can be shown without its experience known.

What it costs
-------------
One request per newly-matching job, once. Not one per job — the pass runs over
title-stage matches only, which is 36 of 788 open rows on this table, and each
of those is asked exactly once for as long as the posting exists. The politeness
is the shared kind: fetchers.http's client, its global semaphore, its per-host
pacing and its 429/5xx backoff, with Workday asking for and getting a slower
pace than the default.

Retries, and why they are rare on purpose
-----------------------------------------
A fetch that returns nothing marks the row desc_fetch_failed, and a failed row
is asked again once RETRY_AFTER has passed — not on the next crawl. Thirty
minutes is the crawl interval; a board that was down thirty minutes ago is
usually still down, and hammering it would be the opposite of what the rest of
this project does. A day later is a real second chance.

Whose filter decides what is worth a request — Phase 2b
-------------------------------------------------------
Nobody's in particular, and that is the change. This pass used to resolve "the
current user" from MY_EMAIL through db/users.py and narrow itself to that one
person's filter, which made the last stage of a user-blind crawl quietly
user-aware. It was harmless with one account and wrong with two: a second user
whose keywords differ would have their matches skipped here, reach their digest
with desc_checked = FALSE — min_experience NULL, and unknown means KEEP — and be
emailed a job on no evidence at all about its experience requirement. That is the
Step 7c leak arriving through a door Phase 1 left open.

The fix is not to enrich per user but to stop asking. A description is worth
fetching if it is worth fetching for ANYBODY, so the pass runs over the UNION of
every active account's filter (notify/store.load_active_preferences) and a job is
a candidate the moment one filter would want it. That keeps the crawl's own
shape: the reading is shared and user-blind — one fetch serves everyone who can
see the job, exactly as one board fetch serves everyone watching the company —
and all the narrowing still happens in the notify stage.

Note what the union does NOT do: it does not widen what anybody is SENT. These
two facts land on the job row, where the matcher was already reading them, and
every per-user decision downstream is unchanged.

What it must never do
---------------------
Touch a lifecycle column. Enrichment writes min_experience, is_pm_role,
desc_checked, desc_fetch_failed and desc_checked_at — and notify/store.py's
UPDATE statement contains no other column, so "it cannot re-open a closed job or
un-send a sent one" is a property of the SQL rather than a promise made here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from notify.description import enrich_candidates
from notify.match import Preferences, is_match, worth_a_description
from notify.store import (
    fetch_enrichment_details,
    load_active_preferences,
    scan_enrichment_targets,
)

# How long a FAILED description read is believed before it is worth asking
# again. Only failures are affected: a description that was read cleanly is
# never re-read, whatever this says.
RETRY_AFTER = timedelta(hours=24)


@dataclass
class EnrichmentPass:
    """What one enrichment pass looked at, and what it concluded."""

    considered: int = 0  # open rows with an unread (or retryable) description
    title_stage: int = 0  # ... of those, the ones worth a request
    read: int = 0  # descriptions actually fetched and parsed
    failed: int = 0  # asked, got nothing — kept, and retried tomorrow
    hidden: list[dict[str, Any]] = field(default_factory=list)  # newly disqualified

    @property
    def checked(self) -> int:
        """Rows whose desc_checked was flipped (or refreshed) by this pass."""
        return self.read + self.failed


def _newly_hidden(
    jobs: list[Mapping[str, Any]], filters: Sequence[Preferences]
) -> list[dict[str, Any]]:
    """The enriched jobs that the description just disqualified — for EVERYONE.

    Every job in `jobs` passed the title stage of at least one active filter, so
    before enrichment each of them was a match for at least one person —
    min_experience NULL and is_pm_role FALSE both mean keep. Anything that is now
    a match for nobody was hidden by what its description said, which makes this
    list the pass's actual product: the jobs that will stop appearing on Home and
    stop being emailed.

    "For nobody" is the honest reading of a report line printed once per crawl.
    The facts a description yields are the same for every reader — four years is
    four years, and 18 LPA is 18 LPA — but max_level, the exclusion list and the
    ticked salary bands are not, so a job hidden from one person can survive for
    another. Reporting it as hidden then would describe somebody else's screen.

    THE SALARY PAIR TRAVELS ON THIS REPORT AND ALMOST NEVER DECIDES IT. An
    unpriced posting passes notify.match.salary_ok whatever anybody ticked, and
    every posting on this board is unpriced — so a row appearing here BECAUSE of
    its pay is the rarest of the four causes and the one worth being able to see.
    """
    return [
        {
            "canonical_id": job["canonical_id"],
            "company": job.get("company"),
            "title": job.get("title"),
            "min_experience": job.get("min_experience"),
            "is_pm_role": bool(job.get("is_pm_role")),
            "salary_min": job.get("salary_min"),
            "salary_max": job.get("salary_max"),
        }
        for job in jobs
        if not any(is_match(job, prefs) for prefs in filters)
    ]


async def enrich_matches(
    conn,
    *,
    filters: Sequence[Preferences] | None = None,
    fetcher=None,
    now: datetime | None = None,
) -> EnrichmentPass:
    """Read every title-stage match's description once, and store what it says.

    The two stages are the same shape they always were: a free test over
    everything, then a request for what survived. What changed in the work-mode
    slice is WHICH free test — `worth_a_description` rather than `title_stage_ok`,
    wider by one branch so that a job which fails only the city gate but is
    plausibly in India still earns a read. Without that a remote posting outside
    the selected cities could never be discovered to BE remote, and the
    relaxation in city_ok would be unreachable by construction. See that function
    for the circularity and for the country floor that keeps the cost bounded —
    24 extra candidates on the live board rather than 310.

    A job earns a request if ANY active filter would want it. The alternative —
    intersecting the filters — would read fewer descriptions and leave exactly the
    jobs one person cares about unenriched, which is the failure this pass exists
    to close rather than a cheaper version of closing it.

    `filters`, `fetcher` and `now` are injection points for tests. Production
    passes none of them: this is a crawl entrypoint, so it reads the active
    filters once here and the query never repeats mid-pass. There is no user_id
    parameter any more and no user to resolve — see the module docstring.
    """
    if filters is None:
        filters = await load_active_preferences(conn)
    now = now or datetime.now(timezone.utc)
    retry_before = now - RETRY_AFTER

    # Two passes, one purpose kept from the single query it replaced: a free test
    # over the whole unchecked board, then a request for what survived. The first
    # pass is now a CHEAP scan — only the columns worth_a_description reads — so
    # the wide columns cross the pooler for the matches alone rather than for the
    # whole board every crawl. See notify/store.py for why that split exists and
    # why it changes nothing downstream.
    targets = await scan_enrichment_targets(conn, retry_before)
    candidate_ids = [
        job["canonical_id"]
        for job in targets
        if any(worth_a_description(job, prefs) for prefs in filters)
    ]
    candidates = await fetch_enrichment_details(conn, candidate_ids)

    report = await enrich_candidates(
        conn, candidates, fetcher=fetcher, retry_before=retry_before
    )

    result = EnrichmentPass(
        considered=len(targets),
        title_stage=len(candidates),
        read=sum(1 for entry in report if entry["fetched"]),
        failed=sum(1 for entry in report if not entry["fetched"]),
    )
    # enrich_candidates updated the rows in place, so the verdict below is
    # against the facts it just learned rather than against a second SELECT.
    result.hidden = _newly_hidden(candidates, filters)
    return result
