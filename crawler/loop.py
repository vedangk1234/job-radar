"""One company, one crawl: fetch, diff, store — behind three guards.

The guards exist because the expensive failure mode of this tool is not missing
a job, it is crying wolf. An inbox that has ever shown you eighty jobs you
already saw is an inbox you stop opening, and once you stop opening it the tool
is worthless even when it is right. So every guard here is biased the same way:
when the data is doubtful, change nothing and say so.

The three, in the order they must run:

    1. Fetch error -> touch nothing. A 500 is not evidence that a board is
       empty. If an exception were allowed to fall through to the diff, every
       open job would land in `missing`, three bad days would close all of them,
       and the recovery day would re-alert the entire board as "new".

    2. Partial fetch -> touch nothing. This is the one that actually bites: a
       paginator that dies mid-way returns a valid, well-formed, wrong answer
       with no error at all. Nothing downstream can tell 40-of-100 from a
       company that laid off 60 reqs, so we refuse the fetch on shape alone.

    3. Stale close -> only after MISS_LIMIT consecutive misses, never on the
       first absence.

Guards 1 and 2 return before any write, so "changed nothing" is structural
rather than something the writes have to be careful about.

No email is sent from this module and no notification matching happens here.
Brand-new jobs are left with notified=FALSE; that flag is the queue Step 4
reads.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping

from fetchers.normalize import canonical_id
from fetchers.tier1 import TIER1_ATS, fetch as fetch_tier1
from fetchers.tier3 import jsonld_jobpostings, sitemap_jobs

from .diff import diff_snapshots
from .store import bump_missing, close_stale, load_company_ids, mark_new, upsert_seen

# Below this share of the known-open set, a fetch is treated as truncated
# rather than as news. Half is deliberately loose: real boards do shed a third
# of their reqs in a hiring freeze, and a guard that fires on ordinary churn
# would be switched off within a week.
PARTIAL_FETCH_FLOOR = 0.5


@dataclass
class Company:
    """The fields of a companies row that a crawl actually needs."""

    id: int
    name: str
    ats: str
    token: str
    config: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Company":
        """Build from an asyncpg record. JSONB may arrive as text — decode it."""
        config = row["config"]
        if isinstance(config, str):
            try:
                config = json.loads(config)
            except ValueError:
                config = {}
        return cls(
            id=row["id"],
            name=row["name"],
            ats=row["ats"],
            token=row["token"],
            config=config or {},
        )


def _tier1(ats: str) -> Callable[[str, dict], Awaitable[list[dict]]]:
    async def run(token: str, config: dict) -> list[dict]:
        return await fetch_tier1(ats, token)

    return run


async def _jsonld(token: str, config: dict) -> list[dict]:
    return await jsonld_jobpostings(config.get("url") or token)


async def _sitemap(token: str, config: dict) -> list[dict]:
    return await sitemap_jobs(config.get("url") or token)


# ats -> (token, config) -> normalised jobs. Every entry takes the same two
# arguments so the loop never special-cases a vendor.
FETCHERS: dict[str, Callable[[str, dict], Awaitable[list[dict]]]] = {
    **{ats: _tier1(ats) for ats in TIER1_ATS},
    "custom_jsonld": _jsonld,
    "custom_sitemap": _sitemap,
}


@dataclass
class CrawlResult:
    """What one company's crawl did, in the shape the report table wants."""

    company: str
    fetched: int = 0
    truly_new: int = 0
    reappeared: int = 0
    missing: int = 0
    closed: int = 0
    seeding: bool = False
    guard: str | None = None  # set iff a guard tripped and nothing was written

    @property
    def guard_tripped(self) -> bool:
        return self.guard is not None


def log_failure(company: Company, exc: Exception) -> None:
    """GUARD 1 fired: the board could not be read at all."""
    print(
        f"  ! {company.name} ({company.ats}:{company.token}) fetch failed, "
        f"state unchanged — {type(exc).__name__}: {exc}",
        file=sys.stderr,
    )


def log_suspect(company: Company, got: int, had: int) -> None:
    """GUARD 2 fired: the board answered, but with implausibly little."""
    print(
        f"  ! {company.name} ({company.ats}:{company.token}) suspected partial fetch, "
        f"state unchanged — got {got} job(s) against {had} known open "
        f"(floor is {PARTIAL_FETCH_FLOOR:.0%})",
        file=sys.stderr,
    )


async def crawl(
    conn,
    company: Company,
    seeding: bool,
    *,
    fetchers: Mapping[str, Callable[[str, dict], Awaitable[list[dict]]]] | None = None,
) -> CrawlResult:
    """Crawl one company and reconcile the jobs table against what it returned.

    `seeding` is decided by the caller from the company's row count (see
    scripts/crawl.py) and only changes one thing: whether brand-new jobs are
    stored already-notified. On a first-ever crawl a board's entire back
    catalogue looks new, so a seed run marks it notified=TRUE and nothing is
    ever emailed for it. Real alerting starts on crawl #2.

    `fetchers` is an injection point for tests; production uses the module
    dict. Nothing else about the code path differs under test.
    """
    table = fetchers if fetchers is not None else FETCHERS
    result = CrawlResult(company=company.name, seeding=seeding)

    # --- GUARD 1: any fetch error changes nothing ---------------------------
    try:
        raw = await table[company.ats](company.token, company.config)
    except Exception as exc:  # noqa: BLE001 — a bad board must not sink the crawl
        log_failure(company, exc)
        result.guard = f"fetch_error:{type(exc).__name__}"
        return result

    fetched: dict[str, dict] = {
        canonical_id(
            company.ats,
            company.id,
            job.get("native_id"),
            job.get("title"),
            job.get("location"),
        ): job
        for job in raw
    }
    result.fetched = len(fetched)

    open_ids, closed_ids = await load_company_ids(conn, company.id)

    # --- GUARD 2: implausibly small answer changes nothing ------------------
    # Note the `open_ids and` — a company with nothing open yet (a seed run, or
    # a board we have only ever seen empty) has no baseline to be suspicious
    # against, and must not be blocked from seeding.
    if open_ids and len(fetched) < len(open_ids) * PARTIAL_FETCH_FLOOR:
        log_suspect(company, got=len(fetched), had=len(open_ids))
        result.guard = f"partial_fetch:{len(fetched)}<{len(open_ids)}"
        return result

    d = diff_snapshots(open_ids, closed_ids, set(fetched))
    result.truly_new = len(d.truly_new)
    result.reappeared = len(d.reappeared)
    result.missing = len(d.missing)

    # Order matters. upsert_seen runs first because it resets miss_count and
    # reopens returned jobs; running it after bump_missing would be harmless
    # but running close_stale before it would close a job that just came back.
    await upsert_seen(conn, company.id, fetched)
    await mark_new(conn, d.truly_new, notified=seeding)
    await bump_missing(conn, company.id, d.missing)

    # --- GUARD 3: close only after MISS_LIMIT consecutive misses ------------
    result.closed = len(await close_stale(conn, company.id))

    # `reappeared` is deliberately not re-notified: we have seen those ids
    # before, so a job that flapped out and back is not news. `truly_new` on a
    # non-seeding run keeps notified=FALSE, which is exactly how Step 4 finds
    # it. Nothing is emailed from here.
    return result
