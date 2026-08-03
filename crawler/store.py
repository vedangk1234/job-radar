"""Everything that touches the jobs table, and nothing that decides anything.

The jobs table is the crawler's only memory. There is no snapshot table: the
rows themselves are the previous snapshot, which means these helpers must be
careful about which columns they are allowed to overwrite.

Four columns are write-once and are never stomped by a routine re-sighting:

    first_seen_at  our own observation of when a posting appeared. Set by
                   mark_new on brand-new rows only. This is the timestamp the
                   digest will show, and it must not drift when a recruiter
                   edits the title.
    seeded         "this row is back catalogue" — set by mark_new on a company's
                   first-ever crawl. Since Phase 1e this is what keeps a silent
                   seed out of every user's digest, so an upsert clearing it
                   would hand a whole back catalogue to the next send.
    notified       the old global boolean. Set by mark_new once, and still
                   written by notify/store.record_sent; the authority moved to
                   per-user state in Phase 1d and 1g drops this column.
    canonical_id   the identity itself.

Both flags are absent from UPSERT_SEEN's ON CONFLICT clause, which is what makes
"a re-sighting cannot re-queue a job" a property of the SQL rather than a rule
the write path has to remember.

And one column is never written at all: the job description. It is not fetched,
not normalised, not stored. Metadata only — that is what keeps this on a free
Postgres tier instead of a paid one.

SQL portability note: these statements stick to the subset Postgres and SQLite
agree on (numbered placeholders, `ON CONFLICT ... DO UPDATE`, `RETURNING`,
`now()`), so the store-level tests can run against an in-memory SQLite database
in milliseconds instead of against Supabase. The queries that run in production
are then literally the queries the tests exercised, rather than a mock of them.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence

from fetchers.normalize import city_label, iso_or_none

# SQLite caps host parameters at 999 and Postgres at 65535; chunk well below
# both so a company with thousands of open jobs cannot blow the limit.
CHUNK = 400

# Consecutive misses before a job is considered gone. Three, not one: boards
# flicker. A paginator that drops its last page, a vendor cache serving a stale
# response, a req briefly unpublished for an edit — each of those makes a live
# job vanish for exactly one fetch.
MISS_LIMIT = 3

UPSERT_SEEN = """
INSERT INTO jobs (canonical_id, company_id, title, location, city, url,
                  source_posted_at, first_seen_at, last_seen_at,
                  miss_count, status, notified)
VALUES ($1, $2, $3, $4, $5, $6, $7, now(), now(), 0, 'open', FALSE)
ON CONFLICT (canonical_id) DO UPDATE
   SET company_id       = $2,
       title            = $3,
       location         = $4,
       city             = $5,
       url              = $6,
       source_posted_at = $7,
       last_seen_at     = now(),
       miss_count       = 0,
       status           = 'open'
"""
# On conflict: first_seen_at, notified and seeded are absent by design (see
# module docstring). `seeded` is absent from the INSERT list too, taking its
# column default of FALSE — mark_new sets it a moment later for the rows the diff
# actually called new, which is the only place that knows whether this is a
# company's first-ever crawl. status='open' + miss_count=0 is what REOPENS a
# closed job that came back, and what forgives a job that missed one or two
# fetches.


def _chunks(items: Sequence[str]) -> Iterable[Sequence[str]]:
    for start in range(0, len(items), CHUNK):
        yield items[start : start + CHUNK]


def _placeholders(count: int, first: int) -> str:
    """`$3, $4, $5` — an IN-list numbered from `first`."""
    return ", ".join(f"${n}" for n in range(first, first + count))


def _as_timestamp(value: Any) -> datetime | None:
    """Vendor posted-at -> aware datetime, or None.

    asyncpg wants a real datetime for TIMESTAMPTZ, not the ISO string the
    fetchers carry. Anything unparseable becomes None rather than a guess —
    the column is advisory and a NULL there costs us nothing, whereas an
    invented date would show up in a digest as fact.
    """
    text = iso_or_none(value)
    if text is None:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


async def count_jobs(conn, company_id: int) -> int:
    """Rows this company has in the jobs table, open or closed.

    Zero means this company has never been crawled, which is the whole basis of
    silent-seed detection. Counting both states matters: a company whose board
    went quiet and had every job closed still has rows, and must not re-seed.
    """
    return int(await conn.fetchval("SELECT count(*) FROM jobs WHERE company_id = $1", company_id))


async def load_company_ids(conn, company_id: int) -> tuple[set[str], set[str]]:
    """(open_ids, closed_ids) — this company's stored state, as sets."""
    rows = await conn.fetch(
        "SELECT canonical_id, status FROM jobs WHERE company_id = $1", company_id
    )
    open_ids: set[str] = set()
    closed_ids: set[str] = set()
    for row in rows:
        (closed_ids if row["status"] == "closed" else open_ids).add(row["canonical_id"])
    return open_ids, closed_ids


async def upsert_seen(conn, company_id: int, fetched_jobs: Mapping[str, Mapping[str, Any]]) -> int:
    """Record every job the board just returned. Insert if new, refresh if known.

    `fetched_jobs` maps canonical_id -> the normalised job dict from a fetcher.

    Every seen job gets last_seen_at=now(), miss_count=0, status='open'. That
    last pair is doing real work: it reopens a job that was closed and came
    back, and it forgives a job that missed one or two fetches, so a flapping
    board can never accumulate its way to a false close.

    The city label is computed here and stored, because a label is cheap to
    recompute but expensive to backfill. It is stored and NOT used to decide
    anything — store loosely, notify tightly.
    """
    if not fetched_jobs:
        return 0

    records = []
    for canonical_id, job in fetched_jobs.items():
        location = job.get("location")
        records.append(
            (
                canonical_id,
                company_id,
                job.get("title") or "(untitled)",  # title is NOT NULL
                location,
                city_label(location),
                job.get("url") or "",  # url is NOT NULL
                _as_timestamp(job.get("posted_at")),
            )
        )

    await conn.executemany(UPSERT_SEEN, records)
    return len(records)


async def mark_new(conn, ids: Iterable[str], notified: bool) -> int:
    """Stamp first_seen_at and record the silent seed on brand-new rows.

    Called only with diff_snapshots().truly_new, so "brand-new" is already
    established by set membership rather than re-derived from the row.

    `notified` is the silent-seed switch, and since Phase 1e it writes TWO
    columns from that one flag — deliberately, because the flag was always
    carrying two different facts and only one of them belongs to a crawl:

        seeded    "this row is back catalogue". A property of THIS CRAWL:
                  user-blind, permanent, and true for everybody forever. Since
                  1e this is what the digest queue reads, in the exact position
                  notified = FALSE used to sit (notify/store.CANDIDATES_SQL), so
                  a seeded row is never emailed to anyone, ever.
        notified  "somebody was emailed this". A property of a PERSON, which is
                  why 1d moved that authority out of this table altogether.
                  Still written here so the boolean stays an accurate rollback
                  net and the dashboard's NEW/NOTIFIED badge stays honest; 1g
                  drops it.

    Writing both from one parameter is the point rather than a shortcut: on a
    seed run they are the same statement about the same rows, and separating
    them into two writes would invite exactly one of the pair to be forgotten.

    This function still knows nothing about users, and must not. It is handed a
    set of ids and a boolean; who cares about those jobs is a question the notify
    path asks, and the crawl-path invariant tests assert that absence across
    every module a crawl executes — by name, so a docstring cannot break it
    either.

    No email is sent from here.
    """
    ids = list(ids)
    if not ids:
        return 0

    total = 0
    for chunk in _chunks(ids):
        sql = (
            "UPDATE jobs SET first_seen_at = now(), notified = $1, seeded = $1 "
            f"WHERE canonical_id IN ({_placeholders(len(chunk), 2)})"
        )
        rows = await conn.fetch(sql + " RETURNING canonical_id", notified, *chunk)
        total += len(rows)
    return total


async def bump_missing(conn, company_id: int, missing_ids: Iterable[str]) -> int:
    """miss_count += 1 for jobs that were open last time and absent now.

    Only increments; closing is a separate decision made by close_stale, and
    keeping the two apart is what makes "absent once" recoverable. Scoped to
    still-open rows so an already-closed job cannot drift upward forever.
    """
    ids = list(missing_ids)
    if not ids:
        return 0

    total = 0
    for chunk in _chunks(ids):
        sql = (
            "UPDATE jobs SET miss_count = miss_count + 1 "
            "WHERE company_id = $1 AND status = 'open' "
            f"AND canonical_id IN ({_placeholders(len(chunk), 2)})"
        )
        rows = await conn.fetch(sql + " RETURNING canonical_id", company_id, *chunk)
        total += len(rows)
    return total


async def close_stale(conn, company_id: int) -> list[str]:
    """Close jobs that have now missed MISS_LIMIT consecutive fetches.

    Returns the ids closed. Consecutive is guaranteed elsewhere: upsert_seen
    resets miss_count to 0 on any sighting, so a non-zero count can only mean
    an unbroken run of absences.
    """
    rows = await conn.fetch(
        "UPDATE jobs SET status = 'closed' "
        "WHERE company_id = $1 AND status = 'open' AND miss_count >= $2 "
        "RETURNING canonical_id",
        company_id,
        MISS_LIMIT,
    )
    return [row["canonical_id"] for row in rows]
