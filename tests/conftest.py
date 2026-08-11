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
from notify.digest import DigestError
from notify.match import (
    DEFAULT_CITIES,
    DEFAULT_EXCLUSIONS,
    DEFAULT_EXP_RANGE_HIGH,
    DEFAULT_EXP_RANGE_LOW,
    DEFAULT_KEYWORDS,
    DEFAULT_MAX_LEVEL,
    DEFAULT_SALARY_BANDS,
    DEFAULT_SHOW_NO_EXPERIENCE_JOBS,
    DEFAULT_WORK_MODES,
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
#
# NO `notified` COLUMN, since Phase 1g dropped it from the real table. The mirror
# has to track the live schema in both directions: a column here that production
# does not have would let a test arrange a state the database cannot hold, and
# pass while asserting something untrue of anything.
#
# work_mode / salary_min / salary_max are the Phase-3 Step-0 columns and
# salary_checked is the salary slice's addition to them. REAL rather than NUMERIC
# for the pair, and nullable with no default, matching db/schema.sql: NULL is what
# "no salary established" is spelled as on both sides.
#
# THE REAL/NUMERIC DIVERGENCE IS HANDLED IN ONE PLACE, notify.match.salary_of,
# which coerces whatever the driver hands back through Decimal(str(...)). asyncpg
# returns a Decimal and this mirror returns a float, and a comparison between the
# two is a TypeError in the middle of a digest run — the same shape of divergence
# the TEXT[]-as-JSON columns below already have, handled the same way.
#
# salary_checked is `INTEGER NOT NULL DEFAULT 0` because SQLite has no BOOLEAN,
# exactly as is_pm_role and desc_checked above it. The default matters: it is what
# makes an unenriched fixture row say "nobody has looked for a salary here",
# which is the honest starting state and the one the parser tests need.
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
  seeded           INTEGER NOT NULL DEFAULT 0,
  min_experience    INTEGER,
  is_pm_role        INTEGER NOT NULL DEFAULT 0,
  desc_checked      INTEGER NOT NULL DEFAULT 0,
  desc_fetch_failed INTEGER NOT NULL DEFAULT 0,
  desc_checked_at   TEXT,
  applied           INTEGER NOT NULL DEFAULT 0,
  applied_at        TEXT,
  work_mode         TEXT,
  salary_min        REAL,
  salary_max        REAL,
  salary_checked    INTEGER NOT NULL DEFAULT 0
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
#
# NO CHECK (id = 1) any more, mirroring the DROP CONSTRAINT in db/schema.sql.
# Phase 1e took the one-row cap off the table because a fan-out needs a second
# FILTER, not just a second user — two users sharing one filter would not test
# isolation at all. One row per USER is still the rule and is still enforced by
# preferences_one_per_user_idx, mirrored below.
#
# The nine Phase-3 Step-0 columns are mirrored for the reason the jobs ones are,
# and with one extra thing to get right: their DEFAULTS are the whole point of
# the migration, so a mirror that omitted them would let a test assert a
# behaviour that only holds because the column is absent. The TEXT[] columns
# become TEXT holding JSON like cities/keywords/exclusions above, so their
# Postgres DEFAULT '{}' is spelled '[]' here — the same divergence
# Preferences._as_tuple already absorbs. Nothing reads any of them yet;
# tests/test_phase3_columns.py is what asserts that.
PREFERENCES_DDL = """
CREATE TABLE preferences (
  id          INTEGER PRIMARY KEY,
  cities      TEXT NOT NULL,
  keywords    TEXT NOT NULL,
  exclusions  TEXT NOT NULL,
  max_level   INTEGER NOT NULL DEFAULT 2,
  updated_at  TEXT,
  user_id     TEXT REFERENCES users(id) ON DELETE CASCADE,
  exp_range_low            INTEGER,
  exp_range_high           INTEGER,
  years_of_experience_held INTEGER,
  show_no_experience_jobs  INTEGER NOT NULL DEFAULT 0,
  selected_cities          TEXT NOT NULL DEFAULT '[]',
  all_india                INTEGER NOT NULL DEFAULT 0,
  work_modes               TEXT NOT NULL DEFAULT '[]',
  salary_bands             TEXT NOT NULL DEFAULT '[]',
  onboarding_completed     INTEGER NOT NULL DEFAULT 0
)
"""

# The partial unique index from db/schema.sql, mirrored because 1e is the first
# sub-phase in which more than one row can exist at all — so it is the first in
# which "one filter per person" is a rule something could actually break.
PREFERENCES_ONE_PER_USER_DDL = """
CREATE UNIQUE INDEX preferences_one_per_user_idx
    ON preferences (user_id) WHERE user_id IS NOT NULL
"""

# The account rows are keyed by. Named `users` here and reached as
# `public.users` by the production statements, which _SCHEMA strips — see the
# note there. The UUID is TEXT because SQLite has no uuid type and nothing in
# the code parses the value; it is passed through as an opaque key.
#
# auth_user_id mirrors the column Phase 2a added — the join to Supabase's
# auth.users, NULL until an account has signed in once. No Python reads it: the
# link is TypeScript, in dashboard/src/lib/link-user.ts. It is here because this
# mirror has to track the live schema in BOTH directions (see the note on
# JOBS_DDL), and because 2c made the column load bearing for something a test
# can state — deleting an account means deleting the row this points AT as well,
# and a mirror missing the column could not express that the two are different
# ids. There is no UNIQUE clause because the production one is a partial index
# (WHERE auth_user_id IS NOT NULL) and nothing in this suite writes a second.
USERS_DDL = """
CREATE TABLE users (
  id            TEXT PRIMARY KEY,
  email         TEXT NOT NULL UNIQUE,
  created_at    TEXT,
  plan          TEXT NOT NULL DEFAULT 'free',
  is_active     INTEGER NOT NULL DEFAULT 1,
  auth_user_id  TEXT
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

# Per-user notification state — the Phase-1d replacement for the global
# jobs.notified boolean as the thing the digest queue READS.
#
# UNIQUE (user_id, canonical_id) is not decoration: it is what every
# ON CONFLICT DO NOTHING in db/notifications.py conflicts against, so without it
# the idempotence tests would pass by inserting duplicates nobody counted.
#
# The job side is canonical_id TEXT, matching production, because `jobs` has no
# id column — its primary key is the canonical_id string. Both ON DELETE CASCADE
# clauses are mirrored verbatim because tests/test_user_notifications.py asserts
# what they do, and the same caveat applies as for user_companies: SQLite ignores
# every foreign key unless PRAGMA foreign_keys is ON, so those tests call
# FakeConn.enforce_foreign_keys() or they assert nothing at all.
# notified_at carries a DEFAULT here as it does in production, and it has to:
# every INSERT in db/notifications.py omits the column deliberately, so a NOT
# NULL column without one would fail on the very first row. CURRENT_TIMESTAMP
# rather than the fake clock's now() because SQLite only accepts constant
# expressions in a DEFAULT — a divergence that costs nothing, because nothing
# anywhere reads this value. If anything ever branches on notified_at, this
# mirror stops being adequate; db/schema.sql explains why nothing may.
USER_JOB_NOTIFICATIONS_DDL = """
CREATE TABLE user_job_notifications (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id      TEXT NOT NULL REFERENCES users(id)          ON DELETE CASCADE,
  canonical_id TEXT NOT NULL REFERENCES jobs(canonical_id) ON DELETE CASCADE,
  notified_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (user_id, canonical_id)
)
"""

# The resume reference — Phase 3 Slice 1. One row per person, pointing at an
# object in a private Supabase Storage bucket.
#
# NO PYTHON READS OR WRITES THIS TABLE, and it is mirrored anyway, for the two
# reasons USERS_DDL.auth_user_id is. First, this mirror tracks the live schema in
# both directions: a table present in Postgres and absent here is a gap that
# hides the next thing to arrive next to it. Second — and this is the one that
# earns its place — the table exists to be CASCADED, and a cascade is exactly the
# kind of claim a test can make and a comment cannot. tests/test_resume_cascade.py
# deletes a user and asserts the row goes with them, under
# FakeConn.enforce_foreign_keys().
#
# What it deliberately does NOT mirror is the object in the bucket. That is not a
# row in any database, it does not cascade, and pretending otherwise here would
# manufacture confidence in the one guarantee this schema cannot make — see the
# storage note in db/schema.sql and the ordering argument in
# dashboard/src/lib/account.ts.
RESUMES_DDL = """
CREATE TABLE resumes (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id       TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  bucket_path   TEXT NOT NULL,
  filename      TEXT NOT NULL,
  content_type  TEXT NOT NULL,
  size_bytes    INTEGER NOT NULL,
  uploaded_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (user_id)
)
"""

# The address every test resolves the current user from. A .test domain, which
# scripts/githooks/pre-commit allows (RFC 2606/6761) — see test-guard.sh.
TEST_EMAIL = "me@example.test"
TEST_USER_ID = "00000000-0000-4000-8000-000000000001"

# THE SECOND ACCOUNT. Two files need it now — tests/test_fanout.py, which proves
# the fan-out sends each person their own digest, and tests/test_leakage.py,
# which proves nothing crosses between them — so the rig lives here rather than
# in whichever of them happened to be written first. A second user defined twice
# with two ids would let one file's "isolation" hold while the other's leaked.
USER_B_ID = "00000000-0000-4000-8000-00000000000b"
USER_B_EMAIL = "second@example.test"


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
            USER_JOB_NOTIFICATIONS_DDL,
            RESUMES_DDL,
            PREFERENCES_ONE_PER_USER_DDL,
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
        seeded: bool = False,
        min_experience: int | None = None,
        is_pm_role: bool = False,
        desc_checked: bool = False,
        desc_fetch_failed: bool = False,
        desc_checked_at: str | None = None,
        applied: bool = False,
        applied_at: str | None = None,
        work_mode: str | None = None,
        salary_min: float | None = None,
        salary_max: float | None = None,
        salary_checked: bool = False,
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

`seeded=True` is a row the silent seed stored — back catalogue, excluded
        from every user's queue, and a fact about the CRAWL rather than about any
        person. It is not "this was emailed": for that, and for every test that
        means it, use add_sent_job below.

        `work_mode` and the salary trio default to the unenriched state for the
        same reason the five above them do: NULL arrangement, no price, and
        nobody having looked for one is exactly what a crawled row starts life
        as. A test that wants a PRICED job says so — and salary_checked is a
        separate parameter rather than being inferred from salary_min, because
        `salary_checked=True, salary_min=None` is a real and important state
        ("we read it, this posting states no pay") that no inference could
        express. See db/schema.sql for why that distinction has a column.

        There was a `notified` parameter beside it until Phase 1g, when the
        column it wrote went. The pair existed because one boolean had been
        carrying both facts; keeping them as two parameters through 1e and 1f is
        what made it obvious that only one of them was ever a crawl's business.
        """
        stamp = self.clock.tick()
        await self.execute(
            "INSERT INTO jobs (canonical_id, company_id, title, location, city, url, "
            "first_seen_at, last_seen_at, status, seeded, "
            "min_experience, is_pm_role, desc_checked, desc_fetch_failed, desc_checked_at, "
            "applied, applied_at, work_mode, salary_min, salary_max, salary_checked) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $7, $8, $9, $10, $11, $12, $13, $14, "
            "$15, $16, $17, $18, $19, $20)",
            canonical_id,
            company_id,
            title,
            location,
            city if city is not None else city_label(location),
            url or f"https://example.test/jobs/{canonical_id}",
            stamp,
            status,
            1 if seeded else 0,
            min_experience,
            1 if is_pm_role else 0,
            1 if desc_checked else 0,
            1 if desc_fetch_failed else 0,
            desc_checked_at,
            1 if applied else 0,
            applied_at,
            work_mode,
            salary_min,
            salary_max,
            1 if salary_checked else 0,
        )

    async def add_sent_job(
        self, canonical_id: str, *, user_id: str = TEST_USER_ID, **kwargs
    ) -> None:
        """A job that HAS been emailed to this user — the whole state, not half.

        Before Phase 1e a test spelled that `add_job(..., notified=True)`, and
        while there was one user and one global boolean it was true enough. It is
        not any more: "emailed" is a row in user_job_notifications naming a
        person, and nothing else. Through 1e and 1f this helper wrote the boolean
        too, because notify/store.record_sent did; 1g removed that write from
        record_sent, so writing it here would be arranging a state production can
        no longer produce.

        The job row itself is therefore ordinary — not seeded, nothing special
        about it — and the notification row is the entire difference between a job
        this user has been told about and one they have not.
        """
        await self.add_job(canonical_id, **kwargs)
        await self.notify_user(user_id, canonical_id)

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

    async def notify_user(self, user_id: str, canonical_id: str) -> None:
        """One user_job_notifications row, inserted directly.

        Deliberately has no ON CONFLICT clause, for the same reason
        track_company has none: a test about idempotence must run the production
        statement from db/notifications.py, so that what is proven safe to repeat
        is the thing production repeats. This helper is for arranging a state,
        not for exercising one.
        """
        await self.execute(
            "INSERT INTO user_job_notifications (user_id, canonical_id, notified_at) "
            "VALUES ($1, $2, $3)",
            user_id,
            canonical_id,
            self.clock.tick(),
        )

    async def add_resume_for(self, user_id: str, auth_user_id: str | None = None) -> str:
        """One resumes row, inserted directly. Returns the bucket_path.

        The path is built the way dashboard/src/lib/resume.ts builds it — folder
        = the AUTH uid, not this table's user_id — because the difference between
        the two is the whole reason the storage policy works for the Phase-1
        account, and a fixture that used the wrong one would let a test agree
        with a mistake. Defaults to user_id for the ordinary case where they are
        equal; pass auth_user_id to arrange the asymmetric one.
        """
        path = f"{auth_user_id or user_id}/resume.pdf"
        await self.execute(
            "INSERT INTO resumes (user_id, bucket_path, filename, content_type, "
            "size_bytes, uploaded_at) VALUES ($1, $2, $3, $4, $5, $6)",
            user_id,
            path,
            "resume.pdf",
            "application/pdf",
            120_000,
            self.clock.tick(),
        )
        return path

    async def notification_pairs(self) -> set[tuple[str, str]]:
        """Every (user_id, canonical_id) currently in user_job_notifications.

        A set of pairs rather than a count, so a CASCADE test can assert both
        halves of "the right rows went and nothing else did" in one comparison —
        and so a backfill test can assert WHICH jobs were covered rather than how
        many, which is the distinction the migration's gate is built on.
        """
        rows = await self.fetch("SELECT user_id, canonical_id FROM user_job_notifications")
        return {(row["user_id"], row["canonical_id"]) for row in rows}

    async def tracking_rows(self) -> set[tuple[str, int]]:
        """Every (user_id, company_id) pair currently in user_companies.

        A set of pairs rather than a count, so a CASCADE test can assert both
        halves of "the right rows went and nothing else did" in one comparison.
        """
        rows = await self.fetch("SELECT user_id, company_id FROM user_companies")
        return {(row["user_id"], row["company_id"]) for row in rows}

    # WHY BOTH HELPERS BELOW NAME THE THREE EXPERIENCE COLUMNS, AND WHY NOT NAMING
    # THEM WOULD BE A TRAP.
    #
    # The mirror above declares `show_no_experience_jobs INTEGER NOT NULL DEFAULT 0`,
    # matching the real column's `BOOLEAN NOT NULL DEFAULT FALSE`, and the two range
    # ints default to NULL. Those defaults are right for the SCHEMA — they mean
    # "nobody has answered" — and they are wrong for a FIXTURE, because since Phase
    # 3 Slice 2 they no longer describe a filter that behaves like this install's.
    # An unnamed row would come out with an unconfigured range (matches nothing at
    # all) and the toggle off (hides every job whose description was not read, which
    # is 99% of them), so every test that seeds a filter and then asserts an ordinary
    # unenriched job matches would fail — for reasons about the fixture rather than
    # about the thing under test.
    #
    # So the defaults here are notify.match's DEFAULT_* mirror, which reproduces the
    # pre-Slice-2 rule exactly: a test that says nothing about experience gets the
    # behaviour it had before the column existed. A test that wants to exercise the
    # new rule passes the values it means.
    # THE TWO CITY COLUMNS SLICE B ADDED DEFAULT TO "NOT MIGRATED", AND THAT IS
    # DELIBERATELY THE OPPOSITE CHOICE FROM THE THREE EXPERIENCE ONES ABOVE.
    #
    # Those needed explicit values because their schema defaults describe a filter
    # that matches nothing, so an unnamed row would have failed every test for a
    # reason about the fixture. selected_cities has the same blank default and does
    # NOT have that problem, because notify.match.Preferences.effective_cities
    # falls back to `cities` when it is empty — which is exactly the state a real
    # account that has not saved since Slice B is in.
    #
    # So `selected_cities=()` here means "this fixture is a pre-Slice-B row", every
    # existing caller passing `cities=[...]` keeps the behaviour it had, and the
    # fallback is exercised by the whole suite rather than by one test that
    # remembers to. A test that wants the NEW column passes it.
    async def set_preferences(
        self,
        cities: Sequence[str] = DEFAULT_CITIES,
        keywords: Sequence[str] = DEFAULT_KEYWORDS,
        exclusions: Sequence[str] = DEFAULT_EXCLUSIONS,
        max_level: int = DEFAULT_MAX_LEVEL,
        user_id: str | None = TEST_USER_ID,
        exp_range_low: int | None = DEFAULT_EXP_RANGE_LOW,
        exp_range_high: int | None = DEFAULT_EXP_RANGE_HIGH,
        show_no_experience_jobs: bool = DEFAULT_SHOW_NO_EXPERIENCE_JOBS,
        selected_cities: Sequence[str] = (),
        all_india: bool = False,
        work_modes: Sequence[str] = DEFAULT_WORK_MODES,
        salary_bands: Sequence[str] = DEFAULT_SALARY_BANDS,
    ) -> None:
        """Seed the preferences row, TEXT[] columns encoded as JSON.

        `work_modes` DEFAULTS TO ALL THREE and must be named here rather than
        left to the column, for the reason show_no_experience_jobs is: the SQLite
        mirror declares it `NOT NULL DEFAULT '[]'`, and an unnamed row would
        arrive with an EMPTY selection — which matches nothing — so every test
        that seeds a filter and expects an ordinary job to match would fail for a
        reason about the fixture. All three is also what the live row holds
        (scripts/backfill_onboarding_prefs.py), so the fixture and production
        agree about what "nothing is filtered on mode" looks like.

        `salary_bands` DEFAULTS TO ALL SIX for the second half of that reason and
        NOT the first. An unnamed row would arrive empty, and an empty selection
        does NOT match nothing here — notify.match.salary_ok fails OPEN on pay, so
        every existing test would have gone on passing either way. It is named
        anyway because the fixture should say what the live row says
        (scripts/backfill_onboarding_prefs.py wrote all six), and because a
        default that happens to be harmless is not the same as one that was
        chosen — the next person changing salary_ok should find a fixture that
        states its answer rather than one that never had to.

        `user_id=None` writes the row UNOWNED, which is the half-applied
        migration state: db/schema.sql applied, the backfill not yet run. It is
        a parameter rather than a separate helper because that state is a real
        thing production can be in for a minute, and load_preferences is
        supposed to refuse rather than silently fall back to defaults in it.

        `exp_range_low=None` (or high) writes the UNCONFIGURED range, which is the
        other real state: a brand-new account before it has onboarded. It matches
        nothing, deliberately — see notify.match.experience_excluded.

        `selected_cities=()` writes the PRE-SLICE-B row: the city answer is in the
        legacy `cities` column and the gate reaches it through the fallback. See
        the comment above for why that is the default here.

        Still clears the table first, so a test that calls it twice replaces the
        filter rather than colliding with itself. A SECOND user's filter goes
        through add_preferences_for, which does not clear — see there.
        """
        await self.execute("DELETE FROM preferences")
        await self.execute(
            "INSERT INTO preferences (id, cities, keywords, exclusions, max_level, updated_at, "
            "user_id, exp_range_low, exp_range_high, show_no_experience_jobs, "
            "selected_cities, all_india, work_modes, salary_bands) "
            "VALUES (1, $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)",
            json.dumps(list(cities)),
            json.dumps(list(keywords)),
            json.dumps(list(exclusions)),
            int(max_level),
            self.clock.tick(),
            user_id,
            exp_range_low,
            exp_range_high,
            int(bool(show_no_experience_jobs)),
            json.dumps(list(selected_cities)),
            int(bool(all_india)),
            json.dumps(list(work_modes)),
            json.dumps(list(salary_bands)),
        )


    async def add_preferences_for(
        self,
        user_id: str,
        cities: Sequence[str] = DEFAULT_CITIES,
        keywords: Sequence[str] = DEFAULT_KEYWORDS,
        exclusions: Sequence[str] = DEFAULT_EXCLUSIONS,
        max_level: int = DEFAULT_MAX_LEVEL,
        exp_range_low: int | None = DEFAULT_EXP_RANGE_LOW,
        exp_range_high: int | None = DEFAULT_EXP_RANGE_HIGH,
        show_no_experience_jobs: bool = DEFAULT_SHOW_NO_EXPERIENCE_JOBS,
        selected_cities: Sequence[str] = (),
        all_india: bool = False,
        work_modes: Sequence[str] = DEFAULT_WORK_MODES,
        salary_bands: Sequence[str] = DEFAULT_SALARY_BANDS,
    ) -> None:
        """A SECOND user's filter. Adds a row; deletes nothing.

        Only possible at all since Phase 1e dropped CHECK (id = 1) — which is why
        it is a separate helper rather than a flag on set_preferences: the two do
        genuinely different things now, one replacing the install's filter and one
        giving another person their own. preferences_one_per_user_idx still
        refuses a second row for the SAME user, and a test that hits that is
        testing the rule rather than tripping over the fixture.
        """
        next_id = int(await self.fetchval("SELECT COALESCE(max(id), 0) + 1 FROM preferences"))
        await self.execute(
            "INSERT INTO preferences (id, cities, keywords, exclusions, max_level, updated_at, "
            "user_id, exp_range_low, exp_range_high, show_no_experience_jobs, "
            "selected_cities, all_india, work_modes, salary_bands) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)",
            next_id,
            json.dumps(list(cities)),
            json.dumps(list(keywords)),
            json.dumps(list(exclusions)),
            int(max_level),
            self.clock.tick(),
            user_id,
            exp_range_low,
            exp_range_high,
            int(bool(show_no_experience_jobs)),
            json.dumps(list(selected_cities)),
            int(bool(all_india)),
            json.dumps(list(work_modes)),
            json.dumps(list(salary_bands)),
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

    Autouse because the variable is still read in three unrelated places — the
    outbound User-Agent, notify/digest.assert_self_send, and the operator scripts'
    db.users.resolve_current_user_id — so setting it here rather than per-test
    means the suite runs against a developer's real .env without picking up their
    real address, and the value matches the users row conftest seeds.

    It is no longer an input to anything the hourly cron runs. Phase 2b took the
    crawl's enrichment pass and run_digest off it; tests/test_enrich.py deletes the
    variable outright in one test, which is what proves that rather than assuming
    it, and tests/test_identity_bridge.py states where the lookup may still live.

    load_env() reads .env with override=False, so a value already in the process
    environment wins — which is exactly what monkeypatch.setenv puts there.
    """
    monkeypatch.setenv("MY_EMAIL", TEST_EMAIL)


@pytest.fixture
def conn() -> FakeConn:
    return FakeConn()


@pytest.fixture
async def digest_conn() -> FakeConn:
    """One user, one company they watch, and the seeded preferences.

    The ordinary case, as of Phase 1e: a fully migrated database where the filter
    has an owner AND the user's watch list contains the company their jobs belong
    to. The tracking row is not decoration — since 1e the digest queue requires
    it, so without it every test in this suite would read an empty queue and pass
    or fail for a reason none of them is about.

    Tracked with a direct INSERT rather than through db.watchlist.track_companies
    on purpose: that function also suppresses the company's stored history, which
    is exactly right in production and would silence every job a test adds
    afterwards. Tests about tracking-time suppression call the real function.
    """
    conn = FakeConn()
    await conn.add_user()
    await conn.add_company(1, "Testco")
    await conn.track_company(TEST_USER_ID, 1)
    await conn.set_preferences()
    return conn


# --- the two-user rig -----------------------------------------------------
#
# Shared by tests/test_fanout.py and tests/test_leakage.py. The two files ask
# different questions of the same arrangement: the fan-out file asks whether each
# person's digest is computed and sent correctly, the leakage file asks whether
# anything of A's ever appears in B's answer. Neither is the other, and they must
# not be allowed to disagree about what "two users" means.


class RecordingSender:
    """Every send that was attempted, in order, with its recipient and body.

    The injection point run_digest and run_fan_out take in place of
    notify.digest.send_via_resend, so "who got what" is answered by what was
    actually handed to the network rather than by a count kept alongside it.

    `fail_for` makes exactly one recipient's send raise, which is how the
    containment tests arrange one broken account among working ones.
    """

    def __init__(self, fail_for: str | None = None) -> None:
        self.fail_for = fail_for
        self.calls: list[dict] = []

    async def __call__(self, subject, html_body, text_body, *, api_key, from_email, to_email):
        self.calls.append({"subject": subject, "to": to_email, "text": text_body})
        if self.fail_for and to_email == self.fail_for:
            raise DigestError(f"pretend Resend refused {to_email}")
        return f"msg_{len(self.calls)}"

    def to(self, email: str) -> list[dict]:
        return [call for call in self.calls if call["to"] == email]


@pytest.fixture
def verified_sender_env(monkeypatch) -> None:
    """A complete, obviously-fake .env so email_config() never reads the real one.

    NOT autouse, deliberately. The modules that want it declare a one-line autouse
    fixture taking this one, because tests/test_digest.py sets a DIFFERENT
    FROM_EMAIL on purpose and a conftest-wide autouse would be a second value for
    the same variable arriving from two directions.

    FROM_EMAIL is a VERIFIED-looking domain here rather than Resend's sandbox
    sender, and that is the difference between testing a fan-out and testing one
    account. With the sandbox sender, Resend delivers only to the address owning
    the API key, so every user but MY_EMAIL is refused before the network call —
    which is the live install's actual state and is asserted directly in
    tests/test_fanout.test_the_sandbox_sender_still_refuses_everyone_but_my_email,
    with the sandbox FROM put back for that one test.
    """
    monkeypatch.setenv("RESEND_API_KEY", "re_test_key")
    monkeypatch.setenv("FROM_EMAIL", "digest@job-radar.test")
    monkeypatch.setenv("MY_EMAIL", TEST_EMAIL)


@pytest.fixture
async def two_users(digest_conn) -> FakeConn:
    """Two people, two companies, two filters, two disjoint watch lists.

    A watches Testco (id 1) for backend roles in Bengaluru; B watches Otherco
    (id 2) for machine-learning roles in Pune. Nothing about the two overlaps, so
    any leak between them is unambiguous rather than a matter of interpretation.

    The tracking rows go in directly rather than through track_companies, because
    that function suppresses the company's stored history — correct in production
    and fatal to a fixture, which adds its jobs afterwards. The one test that is
    ABOUT that suppression calls the real function on top of this.
    """
    await digest_conn.add_company(2, "Otherco")
    await digest_conn.add_user(USER_B_ID, USER_B_EMAIL)
    await digest_conn.track_company(USER_B_ID, 2)

    await digest_conn.set_preferences(cities=["blr"], keywords=["backend"])
    await digest_conn.add_preferences_for(
        USER_B_ID, cities=["pune"], keywords=["machine learning"]
    )
    return digest_conn


async def same_filter_as_a(conn) -> None:
    """Give B the filter A has, replacing the disjoint one the fixture set.

    For the tests where the two users must differ in exactly ONE way — the watch
    list, or nothing at all. Leaving them with different filters there would let
    a leak hide behind "well, B's filter did not want it anyway".
    """
    await conn.execute("DELETE FROM preferences WHERE user_id = $1", USER_B_ID)
    await conn.add_preferences_for(USER_B_ID, cities=["blr"], keywords=["backend"])


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
