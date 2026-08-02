"""Everything the digest touches in the database, and nothing that decides anything.

Five operations, and the split between them is the whole safety story:

    load_preferences           read the filter
    fetch_candidates           read the queue  (notified=FALSE AND status='open')
    fetch_enrichment_targets   read the jobs whose description is unread
    save_description_facts     write what reading a description taught us
    record_sent                write the receipt, ONLY after Resend returned an id

save_description_facts is the only write in this file that is not a receipt, and
it writes four scalars and a clock reading: min_experience, is_pm_role,
desc_checked, desc_fetch_failed, desc_checked_at. It has no parameter for the
description itself, which is the point — see notify/description.py. There is no
column for one and no statement here that could fill it.

fetch_enrichment_targets is read by the CRAWLER (crawler/enrich.py) rather than
by the digest, which is why it does not filter on `notified`: the dashboard
shows every open job that fits, emailed or not, so every open job that fits has
to have had its description read. That difference is the whole bug fix; the
digest's own queue is still fetch_candidates and still ends at notified=FALSE.

record_sent is deliberately the last thing that happens in a digest run, after
the network call has already succeeded. That ordering is the same idea as the
crawler's Guard 1: when the outcome is in doubt, change nothing. A send that
fails leaves every row at notified=FALSE, so the next run simply tries again —
whereas flipping the flag first and then failing would lose the job silently and
forever, which is the one failure this whole table exists to prevent.

The two writes inside record_sent run in one transaction, so it is impossible to
end up with a job marked notified=TRUE that has no row in the sent-log.

SQL portability note (same as crawler/store.py): these statements stay inside
the subset Postgres and SQLite agree on — numbered placeholders, `now()`, plain
joins — so the tests run the production queries against an in-memory database
rather than a mock of them.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence

from .match import Preferences

# Same reasoning as crawler/store.py: SQLite caps host parameters at 999 and
# Postgres at 65535, so chunk well below both.
CHUNK = 400

PREFERENCES_SQL = (
    "SELECT cities, keywords, exclusions, max_level FROM preferences WHERE user_id = $1"
)

# Asked only when the statement above found nothing, and only to tell two very
# different silences apart. See load_preferences.
UNOWNED_PREFERENCES_SQL = "SELECT count(*) FROM preferences WHERE user_id IS NULL"

# The queue, joined to its companies so the digest can group by employer without
# a second round trip. Ordered by company then title so the rendered email is
# stable: the same match set always produces the same HTML.
#
# c.ats / c.token / c.config come along because the second-stage filter has to
# ask the right vendor API for this job's description, and that is the company's
# knowledge, not the job's. The three description-derived columns come along so
# is_match can apply them without a second query — and so a job already checked
# is visibly already checked, which is what stops it being fetched twice.
_CANDIDATES_TEMPLATE = """
SELECT j.canonical_id, j.title, j.location, j.city, j.url,
       j.first_seen_at, j.company_id,
       j.min_experience, j.is_pm_role, j.desc_checked,
       j.desc_fetch_failed, j.desc_checked_at,
       c.name AS company, c.ats, c.token, c.config
  FROM jobs j
  JOIN companies c ON c.id = j.company_id
 WHERE j.status = 'open'{queue_filter}
 ORDER BY c.name, j.title
"""

CANDIDATES_SQL = _CANDIDATES_TEMPLATE.format(queue_filter="\n   AND j.notified = FALSE")

# The same query minus the queue filter. Used only by scripts/preview_digest.py
# --ignore-notified, which never writes: it answers "what WOULD my filter have
# picked out of everything currently open", against a table whose 300 seeded
# rows are all already notified=TRUE.
CANDIDATES_IGNORING_NOTIFIED_SQL = _CANDIDATES_TEMPLATE.format(queue_filter="")

# Every open job whose description we have not successfully read — the crawl's
# enrichment queue, before the title filter narrows it in Python.
#
# The `$1` cutoff is passed in rather than written as `now() - interval '24
# hours'` for the reason the module docstring gives about SQL portability: an
# interval literal is Postgres-only, and this statement has to run verbatim
# against the SQLite shim in tests. It is compared only against rows that
# already failed, so a clean read is never re-asked whatever the cutoff says.
#
# The status = 'open' clause is not an optimisation. A closed job is not worth a
# request, and enriching one would be the crawler spending politeness on a
# posting nobody can apply to.
ENRICHMENT_TARGETS_SQL = """
SELECT j.canonical_id, j.title, j.location, j.city, j.url,
       j.first_seen_at, j.company_id, j.notified,
       j.min_experience, j.is_pm_role, j.desc_checked,
       j.desc_fetch_failed, j.desc_checked_at,
       c.name AS company, c.ats, c.token, c.config
  FROM jobs j
  JOIN companies c ON c.id = j.company_id
 WHERE j.status = 'open'
   AND (j.desc_checked = FALSE
        OR (j.desc_fetch_failed = TRUE
            AND (j.desc_checked_at IS NULL OR j.desc_checked_at < $1)))
 ORDER BY c.name, j.title
"""

INSERT_NOTIFICATION_SQL = """
INSERT INTO notifications (canonical_id, sent_at, digest)
VALUES ($1, now(), $2)
"""

# Two facts and three pieces of bookkeeping. Note what is NOT here: any column
# that could hold the description those facts were derived from — and, just as
# deliberately, any column about the job's LIFECYCLE. There is no notified here,
# no status, no miss_count, no first_seen_at. That is what makes it safe to run
# this in the middle of a crawl: enrichment cannot re-open a closed job, cannot
# un-send a sent one, and cannot make a job look newly seen.
#
# desc_checked = TRUE is set unconditionally, including when min_experience came
# back NULL because the fetch failed. "We looked and learned nothing" is a fact
# worth recording — the alternative is re-fetching the same dead endpoint on
# every run for as long as the posting stays open. desc_fetch_failed is what
# keeps that from meaning "and never look again": see ENRICHMENT_TARGETS_SQL.
SAVE_DESCRIPTION_FACTS_SQL = """
UPDATE jobs
   SET min_experience    = $2,
       is_pm_role        = $3,
       desc_checked      = TRUE,
       desc_fetch_failed = $4,
       desc_checked_at   = now()
 WHERE canonical_id = $1
"""


def _chunks(items: Sequence[str]) -> Iterable[Sequence[str]]:
    for start in range(0, len(items), CHUNK):
        yield items[start : start + CHUNK]


def _placeholders(count: int, first: int) -> str:
    """`$2, $3, $4` — an IN-list numbered from `first`."""
    return ", ".join(f"${n}" for n in range(first, first + count))


async def load_preferences(conn, user_id) -> Preferences:
    """Read this user's preferences row.

    Keyed on the owner rather than on the old fixed address (`WHERE id = 1`).
    `user_id` comes from db.users.resolve_current_user_id, which the entrypoints
    call once per run and thread down — see that module on why the bridge is
    temporary.

    A missing row falls back to the seeded defaults rather than raising, so a
    database that has not had the new schema applied yet still produces a
    correct digest instead of a stack trace. It is a fallback, not a default:
    the row is what the dashboard edits, and the row wins whenever it exists.

    THE ONE SILENCE THAT IS NOT A FALLBACK. That fallback is only honest when
    "no row for this user" means what it says. There is a second way to get zero
    rows: db/schema.sql has been applied, adding a nullable user_id, but
    scripts/backfill_preferences_user.py has not run yet — so the real row is
    sitting there unowned and this query cannot see it. Falling back then would
    replace a filter someone tuned with the seeded defaults and email the
    results, which is exactly the "quietly wrong" failure this codebase spends
    its guards on. So the two are told apart with one extra query, asked only in
    the miss path, and the half-applied case raises instead.
    """
    rows = await conn.fetch(PREFERENCES_SQL, user_id)
    if rows:
        return Preferences.from_row(rows[0])

    if await conn.fetchval(UNOWNED_PREFERENCES_SQL):
        raise RuntimeError(
            "preferences.user_id is unset on an existing row: db/schema.sql has "
            "been applied but scripts/backfill_preferences_user.py has not run. "
            "Run it before crawling or sending a digest — until then this user's "
            "filter is unreadable, and falling back to defaults would silently "
            "replace it."
        )

    return Preferences.from_row(None)


async def fetch_candidates(conn, *, ignore_notified: bool = False) -> list[Mapping[str, Any]]:
    """Rows eligible to be considered — before any filtering.

    Eligible means open and unsent. Whether a job is *interesting* is decided
    afterwards, by notify/match.py, over the rows this returns.

    `ignore_notified` is for preview only. Nothing that can write ever sets it.
    """
    sql = CANDIDATES_IGNORING_NOTIFIED_SQL if ignore_notified else CANDIDATES_SQL
    return [dict(row) for row in await conn.fetch(sql)]


async def fetch_enrichment_targets(
    conn, retry_before: datetime
) -> list[Mapping[str, Any]]:
    """Open jobs whose description has not been read successfully — and only those.

    `retry_before` is the cutoff for asking a FAILED read again; a row that was
    read cleanly is never returned whatever it is set to. The caller then
    narrows this to the title-stage matches, which is where the cost argument
    lives: this returns the unchecked half of the board, and only a few dozen of
    those are worth a request.

    Deliberately not filtered on `notified`. See the module docstring.
    """
    rows = await conn.fetch(ENRICHMENT_TARGETS_SQL, retry_before)
    return [dict(row) for row in rows]


async def save_description_facts(
    conn, facts: Iterable[tuple[str, int | None, bool, bool]]
) -> int:
    """Persist (canonical_id, min_experience, is_pm_role, fetch_failed) per job.

    Returns how many rows were handed to the database. Not in a transaction, and
    it does not need to be: each row is independent, every write is idempotent,
    and a run that dies halfway leaves the jobs it did finish correctly marked
    rather than making the next run redo them.

    This is also the one write the digest performs BEFORE a send, which is a
    deliberate exception to this module's usual ordering. It is safe because it
    touches nothing about the job's lifecycle: no notified flag, no receipt. A
    send that fails afterwards leaves these rows enriched and still queued,
    which is exactly what should happen — the facts are true regardless of
    whether an email went out, and re-reading the descriptions to rediscover
    them would be the only cost of not writing them here.
    """
    records = [
        (cid, years, bool(is_pm), bool(failed)) for cid, years, is_pm, failed in facts
    ]
    if not records:
        return 0

    await conn.executemany(SAVE_DESCRIPTION_FACTS_SQL, records)
    return len(records)


async def record_sent(conn, canonical_ids: Iterable[str], digest: str) -> int:
    """Mark exactly these jobs as sent, and log one notifications row each.

    Call this only after a send has succeeded. Returns the number of job rows
    flipped, which the caller should compare against what it emailed.

    Both writes share one transaction: a flag without a receipt would be a job
    we can never explain, and a receipt without a flag would re-send it tomorrow.
    """
    ids = list(canonical_ids)
    if not ids:
        return 0

    flipped = 0
    async with conn.transaction():
        for chunk in _chunks(ids):
            sql = (
                "UPDATE jobs SET notified = TRUE "
                f"WHERE canonical_id IN ({_placeholders(len(chunk), 1)}) "
                "RETURNING canonical_id"
            )
            flipped += len(await conn.fetch(sql, *chunk))

        await conn.executemany(INSERT_NOTIFICATION_SQL, [(cid, digest) for cid in ids])

    return flipped
