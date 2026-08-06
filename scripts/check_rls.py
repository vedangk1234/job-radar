"""Ask the live database whether anon and authenticated can reach anything.

    python scripts/check_rls.py

This is the read-back for the RLS/REVOKE block at the end of db/schema.sql, and
it is the same question asked from the other side: that file states the
intention, this asks Postgres what is actually true. Phase 2a is what makes the
question urgent — NEXT_PUBLIC_SUPABASE_ANON_KEY goes into the browser bundle, so
from then on "what can the anon role do" is a question about the internet rather
than about a role nobody assumes.

SIX PROPERTIES, asked separately because they fail apart:

  1. Every base table in `public` has RLS enabled. With no policies defined that
     is deny-all — anon matches nothing.
  2. anon and authenticated hold no table privileges, so the deny is not merely
     enforced but has nothing to enforce against.
  3. Nor any sequence privileges. anon=rwU on a sequence includes UPDATE, which
     for a sequence is setval.
  4. The default privileges of the CONNECTING ROLE grant them nothing, so the
     next object this project creates arrives closed rather than open.
  5. No table in `public` is owned by some OTHER role whose default privileges
     do grant them — which is the only way a foreign default ACL can ever bite.
  6. The `resumes` bucket is private, RLS is on for storage.objects, and the four
     policies governing that bucket are scoped to `authenticated`, carry an
     auth.uid() folder predicate, and none of them names `anon`.

PROPERTY 6 IS A DIFFERENT SHAPE FROM THE OTHER FIVE, and Phase 3 Slice 1 is what
added it. Those five all assert an ABSENCE: nobody can reach anything, and the
two runtimes work because they connect as postgres and bypass RLS. Storage cannot
be answered that way. This app holds no service-role key, so every upload and
download travels on the signed-in person's own JWT — the policies are not a
second opinion behind a server-side check, they ARE the check, and "there are no
policies" would mean nobody can read their own resume. So property 6 asserts a
PRESENCE (four policies, correctly scoped) alongside the absence that matters
(none of them reachable by anon, and a bucket that is not public).

A public bucket is the failure worth naming twice: it serves every object over a
permanent URL without consulting a policy at all, so it would leave properties
1-5 green, all four policies present, and every resume readable by anyone.

WHY 4 AND 5 ARE TWO QUESTIONS AND NOT ONE, which is the correction this script
had to be rewritten for. A default ACL governs only objects created BY the role
that set it. This database carries two entries per object type for schema
`public`: one set by `postgres` — the role both DSNs use and the role
scripts/init_db.py applies the schema with — and one set by `supabase_admin`,
which is Supabase's own and which `postgres` cannot revoke (it is neither a
member of that role nor a superuser). An earlier version of this file failed on
BOTH, which made it permanently red for a condition nobody here can fix, and a
check that cannot go green is a check that gets ignored.

The honest form of the question is therefore ownership. Every table in `public`
is owned by `postgres`, so the `supabase_admin` entry governs nothing that
exists and is reported rather than failed. The day a `supabase_admin`-owned
table appears in `public`, property 5 goes red — which is exactly the moment it
should, because that is the first object the foreign entry would actually open.

Belt and braces from a different direction: MS-4b removes `public` from the Data
API's exposed schemas, so PostgREST cannot reach any of this regardless of
grants. These statements are the half that survives someone re-flipping a
console toggle.

THE CONNECTING ROLE IS REPORTED, NOT ASSUMED. Both halves of the verdict depend
on who is asking: ALTER DEFAULT PRIVILEGES binds only the role that ran it, and
RLS binds only roles that do not bypass it. A run as the wrong role could report
success about a different set of objects entirely.

It asks over WHATEVER OBJECTS EXIST rather than over a fixed list, deliberately.
A check written against today's eight table names would report success while a
ninth sat exposed, and "the table nobody remembered" is the exact failure here.

Read-only. It changes nothing and is safe to run against production at any time.
Exit code 0 if all five hold, 1 otherwise, so it can gate a deploy.

The verdict lives in `evaluate()`, which is pure — it takes the gathered facts
and returns failures, touching no database and no clock. That is what lets
tests/test_rls.py mutation-confirm the criteria against synthetic inputs,
including the supabase_admin-owned table that must flip property 5 red and that
does not exist to be tested against for real.
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

# Allow `python scripts/check_rls.py` without installing the project as a package.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

EXPOSED_ROLES = ("anon", "authenticated")

# Tables this project did not create and was told not to migrate: the Phase-1d
# and 1g backups of jobs.notified. Reported separately rather than silently
# skipped, and not counted as a failure, because RLS is ON for both and RLS with
# no policy is deny-all — the grants they still carry cannot be exercised. That
# exclusion is CONDITIONAL ON THE RLS STATE rather than on the name alone: if
# RLS ever came off one of them the grants become live and it fails.
NOT_OURS = ("jobs_notified_backup_1g", "jobs_notified_backup_pre1d")

# An aclitem renders as `grantee=privs/grantor`. A grantee of "" is PUBLIC,
# which is not what this asks about, so the names are anchored.
ACL_RE = re.compile(rf"^({'|'.join(EXPOSED_ROLES)})=")

OBJTYPE_LABELS = {"r": "TABLES", "S": "SEQUENCES", "f": "FUNCTIONS", "T": "TYPES"}


# --- the facts ---------------------------------------------------------------

# Base tables only: views and foreign tables are not what Supabase's default
# grants land on, and pg_tables is the same catalogue the Supabase dashboard's
# Database page reads, so the two cannot disagree about what exists.
RLS_SQL = """
SELECT tablename, rowsecurity
  FROM pg_tables
 WHERE schemaname = 'public'
 ORDER BY tablename
"""

# information_schema.role_table_grants resolves grants THROUGH role membership,
# which is what makes it the honest question: a privilege reached by anon being
# granted some other role is still a privilege anon holds, and a query against
# pg_class.relacl alone would miss it.
GRANTS_SQL = """
SELECT grantee, table_name, privilege_type
  FROM information_schema.role_table_grants
 WHERE table_schema = 'public'
   AND grantee = ANY($1::text[])
 ORDER BY grantee, table_name, privilege_type
"""

# Sequences are not tables and do not appear above. relkind = 'S'.
SEQUENCES_SQL = """
SELECT c.relname AS name, c.relacl::text[] AS acl
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = 'public' AND c.relkind = 'S'
 ORDER BY c.relname
"""

# Who owns each table — the join that turns "some other role's default ACL
# grants anon" from a scary-sounding fact into a checkable one.
TABLE_OWNERS_SQL = """
SELECT c.relname AS name, pg_get_userbyid(c.relowner) AS owner
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = 'public' AND c.relkind = 'r'
 ORDER BY c.relname
"""

# defaclobjtype is Postgres's internal "char" type, which asyncpg hands back as
# BYTES (b'r'), not str — so it is cast to text here. Left uncast it misses
# every OBJTYPE_LABELS lookup and then blows up formatting, which is exactly
# what it did on the first live run.
DEFAULT_ACL_SQL = """
SELECT d.defaclobjtype::text AS objtype,
       pg_get_userbyid(d.defaclrole) AS set_by,
       d.defaclacl::text[] AS acl
  FROM pg_default_acl d
  JOIN pg_namespace n ON n.oid = d.defaclnamespace
 WHERE n.nspname = 'public'
 ORDER BY pg_get_userbyid(d.defaclrole), d.defaclobjtype
"""

# A policy is not a failure — it is a deliberate act. But an unnoticed one turns
# deny-all into something narrower, so they are reported rather than judged.
#
# `public` ONLY, deliberately, even though Slice 1 added four policies one schema
# over. Deny-all is the design in `public` and any policy there widens it; in
# `storage` a policy is the mechanism itself, so the two are asked by two queries
# and judged by two different rules. A single sweep would have to report the four
# storage policies as anomalies in the same breath as it demands they exist.
POLICIES_SQL = """
SELECT tablename, policyname, cmd, roles::text AS roles
  FROM pg_policies
 WHERE schemaname = 'public'
 ORDER BY tablename, policyname
"""

# Property 6. The bucket's own row: `public` is the column that can make every
# policy below it decorative.
#
# LEFT JOIN-shaped as a scalar subquery rather than an inner join, so a MISSING
# bucket comes back as a row of NULLs rather than as no row at all — "the
# migration never ran" and "the bucket is fine" must not both look like an empty
# result set.
BUCKET_SQL = """
SELECT (SELECT b.public FROM storage.buckets b WHERE b.id = 'resumes') AS is_public,
       EXISTS (SELECT 1 FROM storage.buckets b WHERE b.id = 'resumes') AS exists,
       (SELECT c.relrowsecurity
          FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'storage' AND c.relname = 'objects') AS objects_rls
"""

# Every policy on storage.objects, with the roles each is granted to. `roles`
# renders as a Postgres array literal — '{authenticated}' — so an anon-scoped
# policy is visible as text rather than needing a second query.
STORAGE_POLICIES_SQL = """
SELECT policyname, cmd, roles::text AS roles, qual, with_check
  FROM pg_policies
 WHERE schemaname = 'storage' AND tablename = 'objects'
 ORDER BY policyname
"""

# The four operations a resume policy set has to cover.
EXPECTED_STORAGE_COMMANDS = ("SELECT", "INSERT", "UPDATE", "DELETE")

# WHY THIS IS MATCHED BY PROPERTY AND NEVER BY NAME.
#
# The first version of this check keyed on policyname and expected exactly
# `resumes_owner_select` and friends. Two of the live policies were created
# through the Supabase UI, which appends a suffix — `resumes_owner_select
# i5g8va_0` — so the check went red against a database that was completely
# correct. That is the failure mode a security check can least afford: one that
# cries wolf gets switched off, and this is the check standing between a resume
# and the internet.
#
# The deeper problem is that the name was never the thing worth asserting. A
# policy called `resumes_owner_select` that had lost its auth.uid() predicate
# would have passed a name check while letting every signed-in user read every
# resume; a policy called `xyzzy` with the right predicate is perfectly safe. So
# a policy is identified by WHAT IT GOVERNS — it mentions the resumes bucket —
# and then judged on what it actually says.
#
# The strings below are matched against pg_policies' deparsed output, which
# normalises `bucket_id = 'resumes'` to `(bucket_id = 'resumes'::text)` and
# `auth.uid()::text` to `((auth.uid())::text)`. Each probe is chosen to survive
# that rewriting rather than to look like the source.
BUCKET_PREDICATE = "bucket_id = 'resumes'"
OWNER_PREDICATES = ("auth.uid()", "storage.foldername(name)")


def policy_clauses(policy) -> str:
    """A policy's USING and WITH CHECK expressions as one searchable string.

    Both, because which one carries the predicate depends on the operation: an
    INSERT policy has only WITH CHECK, a SELECT or DELETE policy has only USING,
    and UPDATE should have both. Reading one of them would silently exempt
    whichever policies keep their rule in the other.
    """
    return " ".join(filter(None, (policy["qual"], policy["with_check"])))


def resume_policies(storage_policies) -> list:
    """The policies that govern the resumes bucket, whatever they are called."""
    return [p for p in storage_policies if BUCKET_PREDICATE in policy_clauses(p)]

# If this is not BYPASSRLS the locks would apply to US, which is the one way
# this change could break the crawler — so it is checked rather than assumed.
ROLE_SQL = """
SELECT current_user AS role_name,
       (SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user) AS bypassrls
"""


# --- the verdict, pure -------------------------------------------------------


def granting_roles(default_acls) -> dict[str, list[str]]:
    """{role: [object types]} for every default ACL that grants an exposed role.

    Keyed by the role that SET the default, because that is the role whose
    creations it governs. A role absent from this mapping cannot open anything.
    """
    out: dict[str, list[str]] = {}
    for row in default_acls:
        if any(ACL_RE.match(a) for a in (row["acl"] or [])):
            label = OBJTYPE_LABELS.get(row["objtype"], row["objtype"])
            out.setdefault(row["set_by"], []).append(label)
    return out


def evaluate_storage(bucket, storage_policies) -> tuple[list[str], list[str]]:
    """Property 6, kept separate because it judges a PRESENCE. Pure.

    Split out rather than folded into evaluate() for the reason the module
    docstring gives: every other property here asserts that nobody can reach
    anything, and this one asserts that exactly one person can. Two rules that
    read the same way would be two rules a maintainer conflates.

    The checks, in the order a failure would actually hurt:

      - the bucket exists at all (the manual block was applied)
      - it is PRIVATE (a public bucket ignores every policy below)
      - storage.objects enforces RLS (policies on a table with RLS off are inert)
      - one policy per operation governs the bucket, each scoped to
        `authenticated`, each carrying the auth.uid() folder predicate, and none
        of them naming `anon` — which is what makes the published key a no-op

    MATCHED BY PROPERTY, NEVER BY NAME. See the note on BUCKET_PREDICATE above:
    two of the live policies carry a UI-appended suffix, and a name was never the
    thing worth asserting anyway.
    """
    failures: list[str] = []
    notes: list[str] = []

    if not bucket or not bucket["exists"]:
        failures.append(
            "storage.buckets has no row with id = 'resumes' — the storage lock at "
            "the end of db/schema.sql has not been applied, so resume upload has "
            "nowhere to write"
        )
        return failures, notes

    if bucket["is_public"]:
        failures.append(
            "the 'resumes' bucket is PUBLIC. Every object in it is served over a "
            "permanent URL with no policy consulted, so the four policies below "
            "decide nothing and every resume is readable by anyone"
        )

    if not bucket["objects_rls"]:
        failures.append(
            "RLS is OFF on storage.objects, so the resumes_owner_* policies are "
            "inert and every authenticated user can read every object"
        )

    ours = resume_policies(storage_policies)

    if not ours:
        failures.append(
            "no policy on storage.objects governs the 'resumes' bucket. The MANUAL-ONLY "
            "block at the end of db/schema.sql has not been applied, so nobody — "
            "including the owner — can reach a resume"
        )
        return failures, notes

    by_command: dict[str, list] = {}
    for policy in ours:
        by_command.setdefault(policy["cmd"], []).append(policy)

    for cmd in EXPECTED_STORAGE_COMMANDS:
        matching = by_command.get(cmd, [])
        if not matching:
            failures.append(
                f"no {cmd} policy governs the resumes bucket — {cmd} on a resume is "
                f"denied even to the person who owns it"
            )
            continue
        if len(matching) > 1:
            # Two policies for one operation are OR-ed, so the broader one wins
            # and the stricter one is decoration. A failure rather than a note:
            # it is how a temporary debugging policy quietly becomes the rule.
            failures.append(
                f"{len(matching)} policies cover {cmd} on the resumes bucket "
                f"({', '.join(repr(p['policyname']) for p in matching)}). Policies are "
                f"OR-ed together, so the most permissive one decides"
            )

        for policy in matching:
            name = policy["policyname"]
            clauses = policy_clauses(policy)

            if "authenticated" not in (policy["roles"] or ""):
                failures.append(
                    f"storage policy {name!r} ({cmd}) is not scoped TO authenticated "
                    f"(roles = {policy['roles']})"
                )

            # The owner predicate IS the isolation. A policy that kept its name,
            # its operation and its bucket check but lost this would let every
            # signed-in user read every resume — and would pass any check that
            # asked only whether four correctly-named policies existed.
            missing = [p for p in OWNER_PREDICATES if p not in clauses]
            if missing:
                failures.append(
                    f"storage policy {name!r} ({cmd}) does not scope objects to their "
                    f"owner — no {', '.join(missing)} in its predicate: "
                    f"{clauses or '(none)'}"
                )

            if cmd == "UPDATE" and not policy["with_check"]:
                failures.append(
                    f"storage policy {name!r} has no WITH CHECK. USING decides which "
                    f"rows may be updated, WITH CHECK decides what they may become — "
                    f"without it an owner can rename their object into another "
                    f"person's folder"
                )

    # The published key acts as `anon`. No policy touching this bucket may name
    # it, whatever that policy happens to be called.
    anon_scoped = sorted(p["policyname"] for p in ours if "anon" in (p["roles"] or ""))
    if anon_scoped:
        failures.append(
            f"policy/policies {', '.join(repr(n) for n in anon_scoped)} on the resumes "
            f"bucket are scoped to anon — the published "
            f"NEXT_PUBLIC_SUPABASE_ANON_KEY can exercise them"
        )

    # Policies governing some OTHER bucket are somebody else's decision about a
    # table this project now depends on. Reported, not failed: Supabase ships
    # none by default, and a bucket added later for an unrelated feature is not
    # this check's business — but it should be visible.
    others = [p for p in storage_policies if p not in ours]
    if others:
        notes.append(
            f"storage.objects carries {len(others)} policy/policies for other buckets: "
            + ", ".join(f"{p['policyname']!r} ({p['cmd']})" for p in others)
            + " — they do not name the resumes bucket"
        )

    # Names are REPORTED and decide nothing. Two of the live ones carry a suffix
    # the Supabase UI appended; that is cosmetic and deliberately left alone.
    notes.append(
        "resume policies, matched by property rather than by name: "
        + ", ".join(f"{p['policyname']!r} {p['cmd']}" for p in ours)
    )

    return failures, notes


def evaluate(
    *,
    connecting_role: str,
    bypassrls: bool,
    rls_rows,
    grant_rows,
    sequence_rows,
    default_acls,
    table_owners,
) -> tuple[list[str], list[str]]:
    """(failures, notes) for properties 1-5. No database, no clock.

    Pure so the criteria can be mutation-tested against inputs that do not exist
    on the real database — above all a supabase_admin-owned table in `public`,
    which is the condition property 5 is FOR and which cannot be created here to
    test against. Property 6 is evaluate_storage(), above.
    """
    failures: list[str] = []
    notes: list[str] = []
    roles_label = " / ".join(EXPOSED_ROLES)
    rls_by_table = {r["tablename"]: r["rowsecurity"] for r in rls_rows}

    if not bypassrls:
        failures.append(
            f"the connecting role {connecting_role!r} is NOT bypassrls — enabling RLS "
            f"applies to this project's own queries, not just to anon, and any "
            f"ALTER DEFAULT PRIVILEGES applied by a different role did not bind this one."
        )

    # 1. RLS everywhere.
    unprotected = [r["tablename"] for r in rls_rows if not r["rowsecurity"]]
    if unprotected:
        failures.append("RLS is OFF on: " + ", ".join(f"public.{t}" for t in unprotected))

    # 2. No table grants — on tables this schema owns.
    ours = [r for r in grant_rows if r["table_name"] not in NOT_OURS]
    if ours:
        tables = sorted({r["table_name"] for r in ours})
        failures.append(
            f"{len(ours)} table grant(s) still held by {roles_label} across "
            f"{len(tables)} table(s): {', '.join(tables)}"
        )

    # The excluded backups: inert only while RLS denies them.
    theirs = sorted({r["table_name"] for r in grant_rows if r["table_name"] in NOT_OURS})
    still_open = [t for t in theirs if not rls_by_table.get(t)]
    if still_open:
        failures.append(
            f"the excluded backup table(s) {', '.join(still_open)} have RLS OFF *and* "
            f"carry grants to {roles_label} — the exclusion only holds while RLS denies them"
        )
    elif theirs:
        notes.append(
            f"excluded Phase-1 backups {', '.join(theirs)} still carry grants, but RLS is "
            f"ON for each, so {roles_label} cannot exercise them"
        )

    # 3. No sequence grants.
    leaked = [r["name"] for r in sequence_rows if any(ACL_RE.match(a) for a in (r["acl"] or []))]
    if leaked:
        failures.append(
            f"{roles_label} still hold privileges on sequence(s): {', '.join(leaked)} "
            f"— rwU includes setval"
        )

    granting = granting_roles(default_acls)

    # 4. The connecting role's own defaults — what we control.
    if connecting_role in granting:
        failures.append(
            f"the next object THIS project creates would arrive OPEN — the default "
            f"privileges of the connecting role {connecting_role!r} still grant "
            f"{roles_label} on: {', '.join(sorted(granting[connecting_role]))}"
        )

    # 5. Foreign defaults, judged by ownership rather than by existence.
    foreign = {role: types for role, types in granting.items() if role != connecting_role}
    for role, types in sorted(foreign.items()):
        owned = sorted(t["name"] for t in table_owners if t["owner"] == role)
        if owned:
            failures.append(
                f"table(s) {', '.join(owned)} in public are owned by {role!r}, whose "
                f"default privileges grant {roles_label} on {', '.join(sorted(types))} — "
                f"so objects it creates arrive open and this one may already be"
            )
        else:
            notes.append(
                f"{role!r} has default privileges granting {roles_label} on "
                f"{', '.join(sorted(types))}, and they are INERT: a default ACL governs "
                f"only objects created by that role, and no table in public is owned by "
                f"it. {connecting_role!r} cannot revoke them (not a member, not superuser)"
            )

    return failures, notes


# --- the run -----------------------------------------------------------------


async def main() -> int:
    from db.connection import get_crawler_conn

    conn = await get_crawler_conn()
    try:
        role = await conn.fetchrow(ROLE_SQL)
        rls_rows = await conn.fetch(RLS_SQL)
        grant_rows = await conn.fetch(GRANTS_SQL, list(EXPOSED_ROLES))
        sequence_rows = await conn.fetch(SEQUENCES_SQL)
        table_owners = await conn.fetch(TABLE_OWNERS_SQL)
        default_acls = await conn.fetch(DEFAULT_ACL_SQL)
        policy_rows = await conn.fetch(POLICIES_SQL)
        bucket = await conn.fetchrow(BUCKET_SQL)
        storage_policies = await conn.fetch(STORAGE_POLICIES_SQL)
    finally:
        await conn.close()

    roles_label = " / ".join(EXPOSED_ROLES)
    connecting_role = role["role_name"]

    print(f"connected as: {connecting_role}  (rolbypassrls = {role['bypassrls']})")

    print(f"\ntables in public: {len(rls_rows)}")
    owner_of = {t["name"]: t["owner"] for t in table_owners}
    for row in rls_rows:
        name = row["tablename"]
        print(
            f"  {'ON ' if row['rowsecurity'] else 'OFF'}  public.{name}"
            f"  (owner {owner_of.get(name, '?')})"
        )

    ours = [r for r in grant_rows if r["table_name"] not in NOT_OURS]
    theirs = [r for r in grant_rows if r["table_name"] in NOT_OURS]
    print(f"\ntable grants held by {roles_label}: {len(grant_rows)} total")
    print(f"  on tables this schema owns: {len(ours)}")
    for row in ours[:15]:
        print(f"    {row['grantee']} -> {row['table_name']}.{row['privilege_type']}")
    if len(ours) > 15:
        print(f"    ... and {len(ours) - 15} more")
    if theirs:
        names = sorted({r["table_name"] for r in theirs})
        print(f"  on excluded Phase-1 backups: {len(theirs)} across {', '.join(names)}")

    print(f"\nsequences in public: {len(sequence_rows)}")
    for row in sequence_rows:
        held = [a for a in (row["acl"] or []) if ACL_RE.match(a)]
        print(f"  public.{row['name']}  {roles_label}: {' '.join(held) if held else '(none)'}")

    print("\ndefault privileges for schema public:")
    if not default_acls:
        print("  (no pg_default_acl rows — nothing is granted to anyone by default)")
    for row in default_acls:
        label = OBJTYPE_LABELS.get(row["objtype"], row["objtype"])
        held = [a for a in (row["acl"] or []) if ACL_RE.match(a)]
        marker = "  <- ours" if row["set_by"] == connecting_role else ""
        print(
            f"  {label:<10} set_by={row['set_by']:<16} "
            f"{roles_label}: {' '.join(held) if held else '(none)'}{marker}"
        )

    print(f"\npolicies on public.*: {len(policy_rows)}")
    for row in policy_rows:
        print(f"  {row['tablename']}.{row['policyname']} {row['cmd']} roles={row['roles']}")
    if not policy_rows:
        print("  (none — RLS with no policy is deny-all, which is the intent here)")

    print("\nstorage (Phase 3 Slice 1 — the resume bucket):")
    if bucket and bucket["exists"]:
        print(f"  bucket 'resumes'   public={bucket['is_public']}  (must be False)")
        print(f"  storage.objects    RLS={bucket['objects_rls']}")
    else:
        print("  bucket 'resumes'   MISSING")
    print(f"  policies on storage.objects: {len(storage_policies)}")
    for row in storage_policies:
        print(f"    {row['policyname']} {row['cmd']} roles={row['roles']}")

    failures, notes = evaluate(
        connecting_role=connecting_role,
        bypassrls=bool(role["bypassrls"]),
        rls_rows=rls_rows,
        grant_rows=grant_rows,
        sequence_rows=sequence_rows,
        default_acls=default_acls,
        table_owners=table_owners,
    )

    storage_failures, storage_notes = evaluate_storage(bucket, storage_policies)
    failures += storage_failures
    notes += storage_notes

    if notes:
        print("\nREPORTED (not failures):")
        for line in notes:
            print(f"  - {line}")

    if failures:
        print("\nFAILED:", file=sys.stderr)
        for line in failures:
            print(f"  - {line}", file=sys.stderr)
        print(
            "\nApply db/schema.sql (python scripts/init_db.py) — the RLS/REVOKE block "
            "at the end of that file is what closes the parts this project controls.",
            file=sys.stderr,
        )
        return 1

    print(
        f"\nSUCCESS: RLS is on for all {len(rls_rows)} tables in public; {roles_label} "
        f"hold no privileges on any table this schema owns or on any of the "
        f"{len(sequence_rows)} sequences; {connecting_role!r}'s default privileges grant "
        f"them nothing, so the next object this project creates arrives closed; no "
        f"table here is owned by a role whose defaults would open one; and the "
        f"'resumes' bucket is private with all four owner-scoped policies in place "
        f"and none of them reachable by anon."
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception as exc:  # noqa: BLE001 - top-level CLI error reporting
        print(f"\nFAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
