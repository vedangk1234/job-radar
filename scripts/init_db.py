"""Apply db/schema.sql to the Supabase database, then report what now exists.

Reports the tables, and — since Phase 3 Step 0, whose whole migration is twelve
ADD COLUMNs and no new table — asserts a named list of COLUMNS as well. See
EXPECTED_COLUMNS below for why only some columns are worth naming here.

APPLIES MOST OF THE FILE AND ASSERTS THE REST, since Phase 3 Slice 1. The storage
half of that slice — a private bucket and four policies on storage.objects —
needs ownership of a table this project does not own, so it is fenced off inside
db/schema.sql between MANUAL-ONLY markers, applied by hand in the Supabase SQL
editor, and only CHECKED here. See split_manual_block() for the mechanics and the
schema's own OWNERSHIP note for the 42501 that made it necessary.

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

# The storage verdict is IMPORTED rather than restated. scripts/check_rls.py owns
# the definition of "the storage half is correct" and is the thing run routinely;
# this is the apply-time caller of the same function, so a rule that changes there
# changes here. A second copy written for this script is exactly the drift
# npm run match-parity exists to prevent one language over.
#
# check_rls defers its own database import to main(), so importing it here costs
# no connection and needs no environment.
from scripts.check_rls import (  # noqa: E402
    BUCKET_SQL,
    STORAGE_POLICIES_SQL,
    evaluate_storage,
)

SCHEMA_PATH = PROJECT_ROOT / "db" / "schema.sql"

# The fence. Everything between these two markers in db/schema.sql requires
# privileges this project does not have, and is never sent to the database by
# this script. The strings are matched literally, and the schema comment beside
# them says so — changing one means changing the other.
MANUAL_BEGIN = "-- >>> MANUAL-ONLY (storage)"
MANUAL_END = "-- <<< END MANUAL-ONLY"
EXPECTED_TABLES = [
    "companies",
    "jobs",
    "discovered_companies",
    "notifications",
    "preferences",
    "users",
    "user_companies",
    "user_job_notifications",
    "resumes",
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
    # Phase 3 Slice 1. These are read by the app from the day they exist, so
    # their absence is not silent in the way the Step 0 columns' would be — they
    # are asserted anyway because this table's whole job is to point at a file
    # that holds personal data, and "the migration landed" is worth stating for
    # the one table where not landing means an upload route that 500s.
    ("resumes", "bucket_path"),
    ("resumes", "filename"),
    ("resumes", "content_type"),
    ("resumes", "size_bytes"),
    ("resumes", "uploaded_at"),
]

# THE STORAGE HALF, WHICH information_schema CANNOT ANSWER. Slice 1 is the first
# migration in this project whose most important statements are not about a table
# in `public` at all: a private bucket and four policies on storage.objects. A
# run that created `resumes` and silently failed to create those would report
# SUCCESS above while every resume in the bucket was readable by anyone — which
# is the exact failure the whole slice is written to prevent, so it is asked
# here, against pg_policies and storage.buckets, rather than assumed from the
# absence of an error.
#
# scripts/check_rls.py asks the same question as part of its five-property sweep
# and is the thing to run routinely; this is the apply-time half, so a broken
# migration is red at the moment it is applied rather than at the next audit.
def split_manual_block(schema_sql: str) -> tuple[str, str]:
    """(what this script may execute, what a human has to paste).

    THE FENCE EXISTS BECAUSE THE FAILURE WAS NOT LOCAL. asyncpg sends this file
    as one multi-statement execute, which Postgres runs in one implicit
    transaction — so the 42501 raised by `ALTER TABLE storage.objects` did not
    merely skip the storage section, it rolled back the entire migration
    including a table two hundred lines earlier. The error named a storage table
    while the thing that failed to appear was `public.resumes`.

    Removing the block by marker rather than commenting it out keeps the
    statements as real, pasteable SQL that tests/test_rls.py can still parse, and
    keeps db/schema.sql the single description of what this database should look
    like. See the schema's OWNERSHIP note.

    Raises on a malformed fence rather than guessing. A missing END marker would
    otherwise silently swallow the rest of the file, which on this path means
    silently not creating tables.
    """
    if MANUAL_BEGIN not in schema_sql:
        raise ValueError(
            f"db/schema.sql has no {MANUAL_BEGIN!r} marker. The storage section must "
            f"stay fenced — see split_manual_block()."
        )
    before, _, rest = schema_sql.partition(MANUAL_BEGIN)
    manual, sep, after = rest.partition(MANUAL_END)
    if not sep:
        raise ValueError(
            f"db/schema.sql opens a manual block but never closes it with "
            f"{MANUAL_END!r}; refusing to guess where it ends."
        )
    if MANUAL_BEGIN in after:
        raise ValueError(
            "db/schema.sql contains more than one MANUAL-ONLY block. There is one "
            "storage section; a second fence is probably a copy-paste."
        )
    return before + after, manual


def _executable_statements(sql: str) -> str:
    """The executable slice with its `--` comments removed.

    The guard below searches for `storage.`, and this file's prose says the word
    constantly — the whole OWNERSHIP note is about it. Matching raw text would
    make the schema fail its own guard for explaining itself, which is the same
    trap tests/test_rls.py's _statements() exists for.
    """
    return "\n".join(line.split("--")[0] for line in sql.splitlines())


def storage_statements_outside_the_fence(executable_sql: str) -> list[str]:
    """Any statement this script would send that touches Supabase's storage schema.

    THE GUARD THAT STOPS 2026-08-06 HAPPENING TWICE. The fence is only protection
    while everything privileged is inside it, and the natural way to add a fifth
    policy next year is to type it next to the fourth — outside the markers,
    because the markers are easy to miss. This makes that a refusal before the
    connection is opened rather than a rolled-back migration afterwards.
    """
    return [
        line.strip()
        for line in _executable_statements(executable_sql).splitlines()
        if "storage." in line
    ]


async def main() -> int:
    # Deferred so the splitter above can be imported — by tests/test_rls.py, and
    # by anything else that wants to reason about the fence — without a database
    # or a DSN. check_rls.py defers its import for the same reason.
    from db.connection import get_crawler_conn

    if not SCHEMA_PATH.exists():
        print(f"FAILED: schema file not found at {SCHEMA_PATH}", file=sys.stderr)
        return 1

    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")

    try:
        executable_sql, manual_sql = split_manual_block(schema_sql)
    except ValueError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1

    # BEFORE THE CONNECTION, deliberately. If this fires, applying the file would
    # raise 42501 partway through and roll back everything before it — so the
    # useful moment to refuse is while nothing has been sent.
    stray = storage_statements_outside_the_fence(executable_sql)
    if stray:
        print(
            "FAILED: db/schema.sql has statement(s) touching Supabase's `storage` "
            "schema OUTSIDE the MANUAL-ONLY fence:",
            file=sys.stderr,
        )
        for line in stray:
            print(f"  {line}", file=sys.stderr)
        print(
            "\nThis project's role does not own storage.objects, so applying them "
            "raises 42501 — and because the whole file runs in one transaction, "
            "that rolls back every other statement too. Move them inside the "
            f"{MANUAL_BEGIN!r} block and apply them in the Supabase SQL editor.",
            file=sys.stderr,
        )
        return 1

    manual_lines = len([ln for ln in manual_sql.splitlines() if ln.strip()])
    print(
        f"Manual block fenced off: {manual_lines} lines of storage DDL will NOT be "
        f"executed (they are asserted below instead)."
    )

    print("Connecting to Supabase via CRAWLER_DB_URL (session pooler)...")
    conn = await get_crawler_conn()
    try:
        server_version = await conn.fetchval("SELECT version()")
        print(f"Connected: {server_version.split(',')[0]}")

        print(f"Applying schema from {SCHEMA_PATH.relative_to(PROJECT_ROOT)}...")
        await conn.execute(executable_sql)

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

        bucket = await conn.fetchrow(BUCKET_SQL)
        storage_policies = await conn.fetch(STORAGE_POLICIES_SQL)
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

    # THE STORAGE HALF — ASSERTED, NEVER CREATED. Nothing above sent a single
    # storage statement; the fence saw to that. So this is the only thing standing
    # between "the migration reported success" and a bucket of resumes that is
    # either unreachable by its owners or readable by everybody.
    #
    # The verdict comes from scripts/check_rls.evaluate_storage — the same
    # function the routine audit runs, matching policies by property rather than
    # by name, because two of the live ones carry a suffix the Supabase UI added.
    print("\nStorage — asserted, not applied (see the MANUAL-ONLY block in db/schema.sql):")
    if bucket and bucket["exists"]:
        print(f"  bucket 'resumes'   public={bucket['is_public']}  (must be False)")
        print(f"  storage.objects    RLS={bucket['objects_rls']}")
    else:
        print("  bucket 'resumes'   MISSING")
    print(f"  policies on storage.objects: {len(storage_policies)}")
    for row in storage_policies:
        print(f"    {row['policyname']!r}  {row['cmd']}  roles={row['roles']}")

    storage_failures, storage_notes = evaluate_storage(bucket, storage_policies)
    for line in storage_notes:
        print(f"  note: {line}")

    if storage_failures:
        print("\nFAILED — the storage half is not in place:", file=sys.stderr)
        for line in storage_failures:
            print(f"  - {line}", file=sys.stderr)
        print(
            "\nThis script cannot fix it: storage.objects is owned by "
            "supabase_storage_admin and this connection is not. Paste the block "
            "below into the Supabase SQL editor, then run this script again.\n"
            f"{'-' * 72}",
            file=sys.stderr,
        )
        print(manual_sql.strip(), file=sys.stderr)
        print("-" * 72, file=sys.stderr)
        return 1

    print(f"\nSUCCESS: schema applied, all {len(EXPECTED_TABLES)} expected tables "
          f"({', '.join(EXPECTED_TABLES)}) and all {len(EXPECTED_COLUMNS)} asserted "
          f"columns are present. The 'resumes' bucket is private and every "
          f"operation on it is owner-scoped.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception as exc:  # noqa: BLE001 - top-level CLI error reporting
        print(f"\nFAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
