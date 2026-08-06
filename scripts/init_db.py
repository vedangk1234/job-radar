"""Apply db/schema.sql to the Supabase database, then report what now exists.

Reports the tables, and — since Phase 3 Step 0, whose whole migration is twelve
ADD COLUMNs and no new table — asserts a named list of COLUMNS as well. See
EXPECTED_COLUMNS below for why only some columns are worth naming here.

Usage (from anywhere):  python scripts/init_db.py
The schema is idempotent, so running this repeatedly is safe.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Allow `python scripts/init_db.py` without installing the project as a package.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from db.connection import get_crawler_conn  # noqa: E402

SCHEMA_PATH = PROJECT_ROOT / "db" / "schema.sql"
EXPECTED_TABLES = [
    "companies",
    "jobs",
    "discovered_companies",
    "notifications",
    "preferences",
    "users",
    "user_companies",
    "user_job_notifications",
]

# THE COLUMN-LEVEL HALF OF THE SAME QUESTION, added for Phase 3 Step 0.
#
# EXPECTED_TABLES has always answered "did the file create what it says it
# creates", and for eight phases every migration that mattered created a table.
# Step 0 creates none: it is twelve ADD COLUMNs and nothing else, so this script
# would have reported SUCCESS against a database the ALTERs never reached. An
# apply step whose evidence is the absence of an error is not evidence — the
# schema is applied over a pooler from a laptop, and the failure this catches is
# a statement that did not run rather than one that raised.
#
# NOT a list of every column in the schema, deliberately. This asserts the
# columns whose absence would be SILENT — the ones nothing reads yet, so nothing
# else would notice. A column the crawler selects announces its own absence on
# the next crawl; these would sit missing until the feature step that needs them,
# which is the point at which the migration has been forgotten.
EXPECTED_COLUMNS = [
    ("preferences", "exp_range_low"),
    ("preferences", "exp_range_high"),
    ("preferences", "years_of_experience_held"),
    ("preferences", "show_no_experience_jobs"),
    ("preferences", "selected_cities"),
    ("preferences", "all_india"),
    ("preferences", "work_modes"),
    ("preferences", "salary_bands"),
    ("preferences", "onboarding_completed"),
    ("jobs", "work_mode"),
    ("jobs", "salary_min"),
    ("jobs", "salary_max"),
]


async def main() -> int:
    if not SCHEMA_PATH.exists():
        print(f"FAILED: schema file not found at {SCHEMA_PATH}", file=sys.stderr)
        return 1

    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")

    print("Connecting to Supabase via CRAWLER_DB_URL (session pooler)...")
    conn = await get_crawler_conn()
    try:
        server_version = await conn.fetchval("SELECT version()")
        print(f"Connected: {server_version.split(',')[0]}")

        print(f"Applying schema from {SCHEMA_PATH.relative_to(PROJECT_ROOT)}...")
        await conn.execute(schema_sql)

        rows = await conn.fetch(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
            ORDER BY table_name
            """
        )
        tables = [r["table_name"] for r in rows]

        # Asked of information_schema rather than of the file that was just
        # applied: the question is what the DATABASE now holds, and reading it
        # back from the same text that was sent would prove only that the text
        # was consistent with itself.
        column_rows = await conn.fetch(
            """
            SELECT table_name, column_name, data_type, column_default, is_nullable
            FROM information_schema.columns
            WHERE table_schema = 'public'
            """
        )
        columns = {(r["table_name"], r["column_name"]): r for r in column_rows}
    finally:
        await conn.close()

    print(f"\nTables now in public schema ({len(tables)}):")
    for name in tables:
        print(f"  - {name}")

    missing = [t for t in EXPECTED_TABLES if t not in tables]
    if missing:
        print(f"\nFAILED: expected tables missing: {', '.join(missing)}", file=sys.stderr)
        return 1

    print(f"\nAsserted columns ({len(EXPECTED_COLUMNS)}):")
    for table, column in EXPECTED_COLUMNS:
        row = columns.get((table, column))
        if row is None:
            print(f"  MISSING  {table}.{column}")
            continue
        # The default is printed, not merely the presence, because the defaults
        # are what backfill the rows already there — a column that arrived
        # without one is a column every existing row is NULL in.
        default = row["column_default"] or "(none)"
        null = "NULL" if row["is_nullable"] == "YES" else "NOT NULL"
        print(f"  ok       {table}.{column}  {row['data_type']}  {null}  default {default}")

    missing_columns = [f"{t}.{c}" for t, c in EXPECTED_COLUMNS if (t, c) not in columns]
    if missing_columns:
        print(
            f"\nFAILED: expected columns missing: {', '.join(missing_columns)}",
            file=sys.stderr,
        )
        return 1

    print(f"\nSUCCESS: schema applied, all {len(EXPECTED_TABLES)} expected tables "
          f"({', '.join(EXPECTED_TABLES)}) and all {len(EXPECTED_COLUMNS)} asserted "
          f"columns are present.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception as exc:  # noqa: BLE001 - top-level CLI error reporting
        print(f"\nFAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
