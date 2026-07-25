"""Apply db/schema.sql to the Supabase database, then report the tables that exist.

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
EXPECTED_TABLES = ["companies", "jobs", "discovered_companies", "notifications"]


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
    finally:
        await conn.close()

    print(f"\nTables now in public schema ({len(tables)}):")
    for name in tables:
        print(f"  - {name}")

    missing = [t for t in EXPECTED_TABLES if t not in tables]
    if missing:
        print(f"\nFAILED: expected tables missing: {', '.join(missing)}", file=sys.stderr)
        return 1

    print("\nSUCCESS: schema applied and all 4 expected tables are present "
          f"({', '.join(EXPECTED_TABLES)}).")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception as exc:  # noqa: BLE001 - top-level CLI error reporting
        print(f"\nFAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
