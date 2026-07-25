"""Test rig: an in-memory database that speaks enough asyncpg to be honest.

The point of this shim is that the store-level tests run the REAL SQL from
crawler/store.py. A mocked store would happily agree that miss_count goes 1, 2,
3 without ever proving that the actual UPDATE says so — and miss_count is
exactly the kind of thing that breaks quietly and re-alerts an entire board.

So: SQLite in memory, wrapped in the four methods store.py calls
(`fetch`, `fetchval`, `execute`, `executemany`), with two translations —

    $1, $2, ...   ->  ?, in order of appearance (asyncpg numbers its
                      placeholders and may repeat one; SQLite is positional)
    now()         ->  a registered SQL function backed by a fake clock

The clock advances a fixed step on every call and never repeats a value. That
turns "first_seen_at was not overwritten" into a real assertion instead of one
that passes because two writes happened in the same microsecond.

No network, no Postgres, no sleeping. The whole suite runs in milliseconds.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

import pytest

from fetchers.normalize import make_job

DATA_DIR = Path(__file__).resolve().parent / "data"

_PARAM = re.compile(r"\$(\d+)")

# SQLite mirror of the jobs table in db/schema.sql. Same column names, same
# constraints that matter (PK, NOT NULLs, defaults) — the types differ only in
# ways SQLite does not enforce anyway.
JOBS_DDL = """
CREATE TABLE jobs (
  canonical_id     TEXT PRIMARY KEY,
  company_id       INTEGER NOT NULL,
  title            TEXT NOT NULL,
  location         TEXT,
  city             TEXT,
  url              TEXT NOT NULL,
  source_posted_at TEXT,
  first_seen_at    TEXT NOT NULL,
  last_seen_at     TEXT NOT NULL,
  miss_count       INTEGER NOT NULL DEFAULT 0,
  status           TEXT NOT NULL DEFAULT 'open',
  notified         INTEGER NOT NULL DEFAULT 0
)
"""


class FakeClock:
    """Strictly increasing UTC clock. Every tick is a distinct ISO string."""

    def __init__(self, start: datetime | None = None, step_seconds: int = 60) -> None:
        self._now = start or datetime(2026, 7, 1, tzinfo=timezone.utc)
        self._step = timedelta(seconds=step_seconds)

    def tick(self) -> str:
        self._now += self._step
        return self._now.isoformat()


def _translate(sql: str, args: Sequence[Any]) -> tuple[str, list[Any]]:
    """`... $2 ... $1 ... $2` -> `... ? ... ? ... ?` with args in that order."""
    order: list[int] = []

    def replace(match: re.Match[str]) -> str:
        order.append(int(match.group(1)) - 1)
        return "?"

    return _PARAM.sub(replace, sql), [args[i] for i in order]


class FakeConn:
    """The subset of asyncpg.Connection that crawler/store.py actually uses."""

    def __init__(self) -> None:
        self.clock = FakeClock()
        self._db = sqlite3.connect(":memory:", isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.create_function("now", 0, self.clock.tick)
        # Python 3.12 deprecated the implicit datetime adapter; be explicit
        # rather than depend on a shim that is on its way out.
        sqlite3.register_adapter(datetime, lambda d: d.isoformat())
        self._db.execute(JOBS_DDL)

    async def fetch(self, sql: str, *args: Any) -> list[sqlite3.Row]:
        translated, params = _translate(sql, args)
        return self._db.execute(translated, params).fetchall()

    async def fetchval(self, sql: str, *args: Any) -> Any:
        rows = await self.fetch(sql, *args)
        return rows[0][0] if rows else None

    async def execute(self, sql: str, *args: Any) -> None:
        translated, params = _translate(sql, args)
        self._db.execute(translated, params)

    async def executemany(self, sql: str, records: Iterable[Sequence[Any]]) -> None:
        records = list(records)
        if not records:
            return
        translated, _ = _translate(sql, records[0])
        # _translate returns the argument ORDER for one record; apply the same
        # permutation to every record rather than re-parsing the SQL each time.
        order = [int(m.group(1)) - 1 for m in _PARAM.finditer(sql)]
        self._db.executemany(translated, [[rec[i] for i in order] for rec in records])

    # --- helpers for assertions (not part of the asyncpg surface) ----------

    async def snapshot(self) -> dict[str, dict[str, Any]]:
        """Whole jobs table as {canonical_id: row-dict}, for before/after diffs."""
        rows = await self.fetch("SELECT * FROM jobs")
        return {row["canonical_id"]: dict(row) for row in rows}


@pytest.fixture
def conn() -> FakeConn:
    return FakeConn()


# --- job builders ---------------------------------------------------------


def job(native_id: str, title: str = "Engineer", location: str = "Bengaluru, India") -> dict:
    """A synthetic posting in the exact shape every fetcher returns."""
    return make_job(
        native_id=native_id,
        title=title,
        location=location,
        url=f"https://example.test/jobs/{native_id}",
        posted_at="2026-07-01T00:00:00Z",
        department="Engineering",
    )


def load_board(name: str = "greenhouse_board.json") -> list[dict]:
    """Normalise a committed Greenhouse fixture into the canonical row shape.

    A real board, trimmed and checked in, so the seed test runs against real
    titles and real location strings ("Bengaluru-VTP, India", "Remote") rather
    than against strings chosen to be easy to label. fixtures/ itself is
    gitignored, so the suite carries its own copy and stays hermetic.
    """
    raw = json.loads((DATA_DIR / name).read_text(encoding="utf-8"))
    return [
        make_job(
            native_id=row.get("id"),
            title=row.get("title"),
            location=(row.get("location") or {}).get("name"),
            url=row.get("absolute_url"),
            posted_at=row.get("first_published") or row.get("updated_at"),
            department=None,
        )
        for row in raw["jobs"]
    ]


def fetcher_for(*boards: list[dict], ats: str = "greenhouse") -> dict:
    """A FETCHERS table that serves each board in turn, then repeats the last.

    Lets a test spell a multi-run scenario as a list of snapshots.
    """
    queue = list(boards)

    async def run(token: str, config: dict) -> list[dict]:
        return list(queue.pop(0) if len(queue) > 1 else queue[0])

    return {ats: run}


def raising_fetcher(exc: Exception, ats: str = "greenhouse") -> dict:
    async def run(token: str, config: dict) -> list[dict]:
        raise exc

    return {ats: run}
