"""Everything the digest touches in the database, and nothing that decides anything.

Three operations, and the split between them is the whole safety story:

    load_preferences   read the filter
    fetch_candidates   read the queue  (notified=FALSE AND status='open')
    record_sent        write the receipt, ONLY after Resend returned an id

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

from typing import Any, Iterable, Mapping, Sequence

from .match import Preferences

# Same reasoning as crawler/store.py: SQLite caps host parameters at 999 and
# Postgres at 65535, so chunk well below both.
CHUNK = 400

PREFERENCES_SQL = "SELECT cities, keywords, exclusions FROM preferences WHERE id = 1"

# The queue, joined to its companies so the digest can group by employer without
# a second round trip. Ordered by company then title so the rendered email is
# stable: the same match set always produces the same HTML.
_CANDIDATES_TEMPLATE = """
SELECT j.canonical_id, j.title, j.location, j.city, j.url,
       j.first_seen_at, j.company_id, c.name AS company
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

INSERT_NOTIFICATION_SQL = """
INSERT INTO notifications (canonical_id, sent_at, digest)
VALUES ($1, now(), $2)
"""


def _chunks(items: Sequence[str]) -> Iterable[Sequence[str]]:
    for start in range(0, len(items), CHUNK):
        yield items[start : start + CHUNK]


def _placeholders(count: int, first: int) -> str:
    """`$2, $3, $4` — an IN-list numbered from `first`."""
    return ", ".join(f"${n}" for n in range(first, first + count))


async def load_preferences(conn) -> Preferences:
    """Read the single preferences row.

    A missing row falls back to the seeded defaults rather than raising, so a
    database that has not had the new schema applied yet still produces a
    correct digest instead of a stack trace. It is a fallback, not a default:
    the row is what the dashboard edits, and the row wins whenever it exists.
    """
    rows = await conn.fetch(PREFERENCES_SQL)
    return Preferences.from_row(rows[0] if rows else None)


async def fetch_candidates(conn, *, ignore_notified: bool = False) -> list[Mapping[str, Any]]:
    """Rows eligible to be considered — before any filtering.

    Eligible means open and unsent. Whether a job is *interesting* is decided
    afterwards, by notify/match.py, over the rows this returns.

    `ignore_notified` is for preview only. Nothing that can write ever sets it.
    """
    sql = CANDIDATES_IGNORING_NOTIFIED_SQL if ignore_notified else CANDIDATES_SQL
    return [dict(row) for row in await conn.fetch(sql)]


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
