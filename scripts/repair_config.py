"""Re-write double-encoded companies.config values as proper jsonb objects.

    python scripts/repair_config.py            # apply
    python scripts/repair_config.py --dry-run  # show what would change

WHAT IS BEING REPAIRED
----------------------
`companies.config` is jsonb and every reader treats it as an object. A row
written by the dashboard's add-by-URL route before the fix in
dashboard/src/lib/companies.ts holds a jsonb *string* whose contents are JSON
text instead — `jsonb_typeof` says 'string', not 'object'. postgres.js resolved
that parameter to jsonb and applied its own JSON.stringify on top of the
JSON.stringify the route had already done, so the value went in encoded twice.

On the Python side asyncpg returns jsonb as text, so one json.loads gives back a
`str` rather than a dict, and the first `config.get("host")` in the Workday
fetcher raises `'str' object has no attribute 'get'`. Guard 1 catches that and
declines to touch the jobs table, so an affected company crawls to nothing,
forever, while reporting a fetch error rather than a data error. LSEG sat at 0
jobs on that basis.

IDEMPOTENT, BY PREDICATE
------------------------
The UPDATE matches on `jsonb_typeof(config) = 'string'`, which is false the
moment the row is correct. Re-running it changes nothing — there is no flag to
reset and no second application to be careful about. Rows that are already
objects are never touched, which is what keeps this safe to run against the
whole table rather than against a hand-typed id.

A row is only rewritten if the text inside it actually parses to a JSON OBJECT.
That check happens here, in Python, BEFORE the UPDATE: putting it in the WHERE
clause would be relying on Postgres evaluating the AND-ed conditions
left-to-right, which it does not promise, and a cast that raises would take the
whole statement down with it.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crawler.loop import Company  # noqa: E402
from db.connection import get_crawler_conn  # noqa: E402

# `#>> '{}'` extracts a jsonb scalar at the empty path as TEXT — i.e. it strips
# exactly one layer of JSON quoting, which is precisely the extra layer that was
# added. ::jsonb then parses the text that was hiding inside it.
SUSPECT = """
SELECT id, name, ats, token,
       jsonb_typeof(config) AS jtype,
       config::text         AS raw,
       config #>> '{}'      AS unwrapped
  FROM companies
 WHERE jsonb_typeof(config) <> 'object'
 ORDER BY id
"""

REPAIR = """
UPDATE companies
   SET config = $2::jsonb
 WHERE id = $1
   AND jsonb_typeof(config) = 'string'
RETURNING jsonb_typeof(config) AS jtype, config::text AS raw
"""

VERIFY = "SELECT id, name, ats, token, config FROM companies WHERE id = $1"


async def main() -> int:
    dry_run = "--dry-run" in sys.argv[1:]

    conn = await get_crawler_conn()
    try:
        suspects = await conn.fetch(SUSPECT)
        if not suspects:
            print("Every companies.config is already a jsonb object. Nothing to repair.")
            return 0

        print(f"{len(suspects)} row(s) with a non-object config:\n")
        repairable: list[tuple[int, str, str]] = []
        for row in suspects:
            print(f"  id={row['id']}  {row['name']}  ({row['ats']}:{row['token']})")
            print(f"    jsonb_typeof : {row['jtype']}")
            print(f"    stored       : {row['raw'][:120]}")

            if row["jtype"] != "string":
                print("    ! not a quoted string — this is not the double-encoding bug, skipping\n",
                      file=sys.stderr)
                continue

            unwrapped = row["unwrapped"]
            try:
                decoded = json.loads(unwrapped)
            except ValueError as exc:
                print(f"    ! the text inside is not JSON ({exc}) — skipping\n", file=sys.stderr)
                continue
            if not isinstance(decoded, dict):
                print(f"    ! decodes to {type(decoded).__name__}, not an object — skipping\n",
                      file=sys.stderr)
                continue

            print(f"    would store  : {json.dumps(decoded)[:120]}")
            print()
            repairable.append((row["id"], row["name"], unwrapped))

        if not repairable:
            print("Nothing repairable.", file=sys.stderr)
            return 1

        if dry_run:
            print(f"--dry-run: {len(repairable)} row(s) left untouched.")
            return 0

        for company_id, name, unwrapped in repairable:
            written = await conn.fetchrow(REPAIR, company_id, unwrapped)
            if written is None:
                # The predicate stopped matching between the SELECT and the
                # UPDATE, i.e. somebody else repaired it. Not an error.
                print(f"id={company_id} {name}: already an object, left alone")
                continue
            print(f"id={company_id} {name}: config -> {written['jtype']}  {written['raw'][:110]}")

            # The check that matters is not what Postgres stored but what the
            # crawler makes of it, so verify through the real loader.
            company = Company.from_row(await conn.fetchrow(VERIFY, company_id))
            kind = type(company.config).__name__
            print(f"  Company.from_row(...).config is a {kind}: {json.dumps(company.config)[:110]}")
            if not isinstance(company.config, dict):
                print("  ! still not a dict", file=sys.stderr)
                return 1

        left = await conn.fetchval(
            "SELECT count(*) FROM companies WHERE jsonb_typeof(config) <> 'object'"
        )
        print(f"\nrows with a non-object config remaining: {left}")
        return 0 if left == 0 else 1
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
