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

from fetchers.normalize import city_label, make_job
from notify.match import (
    DEFAULT_CITIES,
    DEFAULT_EXCLUSIONS,
    DEFAULT_KEYWORDS,
    DEFAULT_MAX_LEVEL,
)

DATA_DIR = Path(__file__).resolve().parent / "data"

_PARAM = re.compile(r"\$(\d+)")

# Postgres casts a parameter into a jsonb/text column with `$1::jsonb`. SQLite
# has no such syntax and does not need one, so the cast is stripped rather than
# the statement being rewritten for the test — what runs here is otherwise the
# production statement, character for character.
_CAST = re.compile(r"::(?:jsonb|json|text|int|integer|timestamptz)\b", re.IGNORECASE)

# db/users.py and the dashboard both write `public.users` rather than `users`,
# because Supabase ships its own auth.users and a bare name would resolve
# against whatever the connection's search_path happens to be. SQLite has one
# nameless schema and no such ambiguity, so the qualifier is stripped — the same
# kind of translation as the casts above, and for the same reason: what runs
# here stays the production statement in every respect that decides anything.
_SCHEMA = re.compile(r"\bpublic\.", re.IGNORECASE)

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
  notified         INTEGER NOT NULL DEFAULT 0,
  min_experience    INTEGER,
  is_pm_role        INTEGER NOT NULL DEFAULT 0,
  desc_checked      INTEGER NOT NULL DEFAULT 0,
  desc_fetch_failed INTEGER NOT NULL DEFAULT 0,
  desc_checked_at   TEXT,
  applied           INTEGER NOT NULL DEFAULT 0,
  applied_at        TEXT
)
"""

# The rest of db/schema.sql, in the same SQLite dialect. The digest joins jobs
# to companies and writes to notifications, so both have to exist here for the
# production SQL to run at all.
#
# The TEXT[] columns of `preferences` become TEXT holding JSON. That divergence
# is handled once, in Preferences.from_row, which accepts either a real list
# (asyncpg) or a JSON string (here) — the same accommodation Company.from_row
# already makes for JSONB arriving as text.
#
# UNIQUE (ats, token) is not decoration here: it is the constraint every
# "add a company" path relies on to be idempotent, so the ON CONFLICT clauses
# in crawler/discover.py have something real to conflict against.
COMPANIES_DDL = """
CREATE TABLE companies (
  id            INTEGER PRIMARY KEY,
  name          TEXT NOT NULL,
  ats           TEXT NOT NULL DEFAULT 'greenhouse',
  token         TEXT NOT NULL DEFAULT 'testco',
  config        TEXT NOT NULL DEFAULT '{}',
  category      TEXT,
  tracked       INTEGER NOT NULL DEFAULT 0,
  india_scoped  INTEGER NOT NULL DEFAULT 0,
  created_at    TEXT,
  UNIQUE (ats, token)
)
"""

# The discovery script's staging table. `city_hits` is TEXT[] in Postgres and
# TEXT holding JSON here — the same divergence `preferences` already has, and
# handled the same way: an adapter converts a bound list to JSON on the way in
# (see FakeConn), so the production statement binds a real Python list in both.
DISCOVERED_DDL = """
CREATE TABLE discovered_companies (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  name                TEXT,
  ats                 TEXT NOT NULL,
  token               TEXT NOT NULL,
  config              TEXT NOT NULL DEFAULT '{}',
  job_count           INTEGER,
  last_posting_at     TEXT,
  city_hits           TEXT,
  looks_like_services INTEGER NOT NULL DEFAULT 0,
  source              TEXT,
  discovered_at       TEXT,
  promoted            INTEGER NOT NULL DEFAULT 0,
  UNIQUE (ats, token)
)
"""

NOTIFICATIONS_DDL = """
CREATE TABLE notifications (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  canonical_id TEXT NOT NULL,
  sent_at      TEXT NOT NULL,
  digest       TEXT
)
"""

# user_id mirrors the real column: nullable, because that is what makes the
# half-applied-migration state reachable in a test rather than only in
# production. tests/test_digest.py exercises it directly.
PREFERENCES_DDL = """
CREATE TABLE preferences (
  id          INTEGER PRIMARY KEY CHECK (id = 1),
  cities      TEXT NOT NULL,
  keywords    TEXT NOT NULL,
  exclusions  TEXT NOT NULL,
  max_level   INTEGER NOT NULL DEFAULT 2,
  updated_at  TEXT,
  user_id     TEXT REFERENCES users(id) ON DELETE CASCADE
)
"""

# The account rows are keyed by. Named `users` here and reached as
# `public.users` by the production statements, which _SCHEMA strips — see the
# note there. The UUID is TEXT because SQLite has no uuid type and nothing in
# the code parses the value; it is passed through as an opaque key.
USERS_DDL = """
CREATE TABLE users (
  id          TEXT PRIMARY KEY,
  email       TEXT NOT NULL UNIQUE,
  created_at  TEXT,
  plan        TEXT NOT NULL DEFAULT 'free',
  is_active   INTEGER NOT NULL DEFAULT 1
)
"""

# The per-user watch list. Both ON DELETE CASCADE clauses are mirrored verbatim
# from db/schema.sql because tests/test_user_companies.py asserts what they do —
# but note that SQLite ignores every foreign key unless PRAGMA foreign_keys is
# ON, and it is OFF by default. FakeConn.enforce_foreign_keys() is the opt-in;
# see the note there for why it is not simply switched on for the whole suite.
USER_COMPANIES_DDL = """
CREATE TABLE user_companies (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id     TEXT NOT NULL REFERENCES users(id)     ON DELETE CASCADE,
  company_id  INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  created_at  TEXT,
  UNIQUE (user_id, company_id)
)
"""

# The address every test resolves the current user from. A .test domain, which
# scripts/githooks/pre-commit allows (RFC 2606/6761) — see test-guard.sh.
TEST_EMAIL = "me@example.test"
TEST_USER_ID = "00000000-0000-4000-8000-000000000001"


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

    return _SCHEMA.sub("", _CAST.sub("", _PARAM.sub(replace, sql))), [args[i] for i in order]


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
        # asyncpg binds a Python list straight into a TEXT[] column
        # (discovered_companies.city_hits). SQLite has no array type, so the
        # list becomes the JSON the mirrored DDL declares — the binding in the
        # production statement is unchanged either way.
        sqlite3.register_adapter(list, lambda v: json.dumps(v))
        for ddl in (
            JOBS_DDL,
            COMPANIES_DDL,
            NOTIFICATIONS_DDL,
            USERS_DDL,
            PREFERENCES_DDL,
            DISCOVERED_DDL,
            USER_COMPANIES_DDL,
        ):
            self._db.execute(ddl)

    def enforce_foreign_keys(self) -> None:
        """Turn on SQLite's foreign keys. Opt-in, and deliberately not autouse.

        SQLite defaults `PRAGMA foreign_keys` to OFF, so a REFERENCES clause is
        documentation until this is called — which means a test asserting that
        deleting a user removes their tracking rows would simply fail, with the
        rows sitting there untouched.

        Not switched on for the whole suite because the existing tests predate
        every foreign key in the mirrored schema and legitimately seed rows in
        an order no production path uses — set_preferences(user_id=...) without
        a users row, for one, which is how the half-applied-migration case is
        reached. Turning it on globally would fail those for a reason that has
        nothing to do with what they test.
        """
        self._db.execute("PRAGMA foreign_keys = ON")

    def transaction(self) -> "FakeTransaction":
        """asyncpg's `async with conn.transaction():`, backed by real BEGIN/ROLLBACK.

        Real, not a no-op: notify/store.record_sent claims that the flag flip and
        the sent-log insert either both happen or neither does, and a stub
        context manager would let that claim go untested.
        """
        return FakeTransaction(self._db)

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

    async def sent_log(self) -> list[dict[str, Any]]:
        """Every notifications row, oldest first."""
        rows = await self.fetch("SELECT * FROM notifications ORDER BY id")
        return [dict(row) for row in rows]

    async def add_company(
        self,
        company_id: int,
        name: str,
        *,
        ats: str = "greenhouse",
        token: str | None = None,
        tracked: bool = True,
    ) -> None:
        """Insert one companies row. The token defaults to a per-id unique one.

        Unique by default because the table carries UNIQUE (ats, token) exactly
        as production does, so two companies sharing the DDL's default token
        would collide — and a test that wanted two companies would fail for a
        reason that has nothing to do with what it was testing.
        """
        await self.execute(
            "INSERT INTO companies (id, name, ats, token, tracked) VALUES ($1, $2, $3, $4, $5)",
            company_id,
            name,
            ats,
            token or f"testco{company_id}",
            1 if tracked else 0,
        )

    async def add_job(
        self,
        canonical_id: str,
        *,
        company_id: int = 1,
        title: str = "Backend Engineer",
        location: str = "Bengaluru, India",
        city: str | None = None,
        url: str | None = None,
        status: str = "open",
        notified: bool = False,
        min_experience: int | None = None,
        is_pm_role: bool = False,
        desc_checked: bool = False,
        desc_fetch_failed: bool = False,
        desc_checked_at: str | None = None,
        applied: bool = False,
        applied_at: str | None = None,
    ) -> None:
        """Insert one jobs row directly.

        Bypasses the crawler on purpose: these tests are about what the digest
        does with a row, not about how the row got there.

        The five description-derived columns default to exactly what a crawled
        row starts life as — unknown, not a PM role, never read, never failed,
        never looked at — so a test that says nothing about them is testing the
        ordinary case. `applied` defaults the same way, and for the stronger
        reason: a crawled row can only ever start there, because the crawler has
        no statement that writes the column at all.
        """
        stamp = self.clock.tick()
        await self.execute(
            "INSERT INTO jobs (canonical_id, company_id, title, location, city, url, "
            "first_seen_at, last_seen_at, status, notified, "
            "min_experience, is_pm_role, desc_checked, desc_fetch_failed, desc_checked_at, "
            "applied, applied_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16)",
            canonical_id,
            company_id,
            title,
            location,
            city if city is not None else city_label(location),
            url or f"https://example.test/jobs/{canonical_id}",
            stamp,
            status,
            1 if notified else 0,
            min_experience,
            1 if is_pm_role else 0,
            1 if desc_checked else 0,
            1 if desc_fetch_failed else 0,
            desc_checked_at,
            1 if applied else 0,
            applied_at,
        )

    async def add_user(
        self, user_id: str = TEST_USER_ID, email: str = TEST_EMAIL
    ) -> str:
        """Insert one public.users row and return its id."""
        await self.execute(
            "INSERT INTO users (id, email, created_at) VALUES ($1, $2, $3)",
            user_id,
            email,
            self.clock.tick(),
        )
        return user_id

    async def track_company(self, user_id: str, company_id: int) -> None:
        """One user_companies row, inserted directly.

        Deliberately has no ON CONFLICT clause: a test that wants the idempotent
        behaviour runs the production statement from db/watchlist.py instead, so
        the conflict handling under test is the one production actually uses.
        """
        await self.execute(
            "INSERT INTO user_companies (user_id, company_id, created_at) VALUES ($1, $2, $3)",
            user_id,
            company_id,
            self.clock.tick(),
        )

    async def tracking_rows(self) -> set[tuple[str, int]]:
        """Every (user_id, company_id) pair currently in user_companies.

        A set of pairs rather than a count, so a CASCADE test can assert both
        halves of "the right rows went and nothing else did" in one comparison.
        """
        rows = await self.fetch("SELECT user_id, company_id FROM user_companies")
        return {(row["user_id"], row["company_id"]) for row in rows}

    async def set_preferences(
        self,
        cities: Sequence[str] = DEFAULT_CITIES,
        keywords: Sequence[str] = DEFAULT_KEYWORDS,
        exclusions: Sequence[str] = DEFAULT_EXCLUSIONS,
        max_level: int = DEFAULT_MAX_LEVEL,
        user_id: str | None = TEST_USER_ID,
    ) -> None:
        """Seed the preferences row, TEXT[] columns encoded as JSON.

        `user_id=None` writes the row UNOWNED, which is the half-applied
        migration state: db/schema.sql applied, the backfill not yet run. It is
        a parameter rather than a separate helper because that state is a real
        thing production can be in for a minute, and load_preferences is
        supposed to refuse rather than silently fall back to defaults in it.
        """
        await self.execute("DELETE FROM preferences")
        await self.execute(
            "INSERT INTO preferences (id, cities, keywords, exclusions, max_level, updated_at, user_id) "
            "VALUES (1, $1, $2, $3, $4, $5, $6)",
            json.dumps(list(cities)),
            json.dumps(list(keywords)),
            json.dumps(list(exclusions)),
            int(max_level),
            self.clock.tick(),
            user_id,
        )


class FakeTransaction:
    """SQLite BEGIN/COMMIT/ROLLBACK behind asyncpg's async context-manager shape."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db

    async def __aenter__(self) -> "FakeTransaction":
        self._db.execute("BEGIN")
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        self._db.execute("ROLLBACK" if exc_type else "COMMIT")
        return False


@pytest.fixture(autouse=True)
def no_description_fetches(monkeypatch) -> None:
    """No test reaches a real ATS. Description fetching answers None by default.

    Autouse because notify.digest.run_digest now performs a network read between
    its two filter stages, and a suite whose behaviour depends on whether
    boards-api.greenhouse.io is up is not a suite. None is also the honest
    stand-in: it is exactly what a dead board returns in production, so the
    existing digest tests keep asserting the "we could not read it, so keep the
    job" path rather than a path invented for them.

    Tests that need real detection either call the detectors directly on saved
    text or inject their own fetcher — see tests/test_description.py.
    """

    async def offline(company, job):
        return None

    monkeypatch.setattr("notify.description.fetch_description", offline)
    monkeypatch.setattr("notify.description._board_cache", {})


@pytest.fixture(autouse=True)
def current_user_env(monkeypatch) -> None:
    """MY_EMAIL, for every test.

    Autouse because MY_EMAIL is now the input to db.users.resolve_current_user_id
    — the Phase-2 bridge every entrypoint calls to find out whose filter to
    apply. Setting it here rather than per-test means the suite runs against a
    developer's real .env without picking up their real address, and the value
    matches the users row conftest seeds.

    load_env() reads .env with override=False, so a value already in the process
    environment wins — which is exactly what monkeypatch.setenv puts there.
    """
    monkeypatch.setenv("MY_EMAIL", TEST_EMAIL)


@pytest.fixture
def conn() -> FakeConn:
    return FakeConn()


@pytest.fixture
async def digest_conn() -> FakeConn:
    """A FakeConn with one user, one company, and the seeded preferences.

    The preferences row is owned by that user from the start, so every existing
    test goes on describing the ordinary case: a fully migrated database where
    the filter has an owner.
    """
    conn = FakeConn()
    await conn.add_user()
    await conn.add_company(1, "Testco")
    await conn.set_preferences()
    return conn


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
