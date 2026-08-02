"""The crawl's second stage: read the descriptions of the jobs that fit.

    fetch -> diff -> store        (crawler/loop.py, once per company)
    -> ENRICH                     (this module, once per crawl)

Why this is part of a crawl at all
----------------------------------
It was not, and that was the bug. Enrichment used to live only in the digest
path, which reads the notified=FALSE queue — so a description was read the first
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
from typing import Any, Mapping

from db.users import resolve_current_user_id
from notify.description import enrich_candidates
from notify.match import Preferences, is_match, title_stage_ok
from notify.store import fetch_enrichment_targets, load_preferences

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
    jobs: list[Mapping[str, Any]], prefs: Preferences
) -> list[dict[str, Any]]:
    """The enriched jobs that the description just disqualified.

    Every job in `jobs` passed the title stage, so before enrichment each of
    them was a match — min_experience NULL and is_pm_role FALSE both mean keep.
    Anything that is not a match now was hidden by what its description said,
    which makes this list the pass's actual product: the jobs that will stop
    appearing on Home and stop being emailed.
    """
    return [
        {
            "canonical_id": job["canonical_id"],
            "company": job.get("company"),
            "title": job.get("title"),
            "min_experience": job.get("min_experience"),
            "is_pm_role": bool(job.get("is_pm_role")),
        }
        for job in jobs
        if not is_match(job, prefs)
    ]


async def enrich_matches(
    conn,
    *,
    user_id=None,
    prefs: Preferences | None = None,
    fetcher=None,
    now: datetime | None = None,
) -> EnrichmentPass:
    """Read every title-stage match's description once, and store what it says.

    The two stages are the same seam the digest uses, and for the same reason:
    `title_stage_ok` is free and runs over everything, `enrich_candidates` costs
    a request and runs over what survived. Doing it in this order is what keeps
    the pass at a few dozen requests instead of 788.

    `user_id`, `prefs`, `fetcher` and `now` are injection points for tests.
    Production passes none of them: this is a crawl entrypoint, so it resolves
    the current user once here and the lookup never repeats mid-pass. A caller
    that already has `prefs` skips the resolution entirely — there is nothing
    left to look a user up for.
    """
    if prefs is None:
        if user_id is None:
            user_id = await resolve_current_user_id(conn)
        prefs = await load_preferences(conn, user_id)
    now = now or datetime.now(timezone.utc)
    retry_before = now - RETRY_AFTER

    targets = await fetch_enrichment_targets(conn, retry_before)
    candidates = [job for job in targets if title_stage_ok(job, prefs)]

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
    result.hidden = _newly_hidden(candidates, prefs)
    return result
