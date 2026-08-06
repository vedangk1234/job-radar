"""The anon key opens nothing — asserted against the statements, not the database.

Phase 2a puts NEXT_PUBLIC_SUPABASE_ANON_KEY in the browser bundle. That is what
the key is for, and it is safe exactly as long as the anon role can reach
nothing: RLS enabled with no policy is deny-all, and REVOKE means the privilege
is not merely unusable but absent. db/schema.sql ends with the block that sets
both.

WHAT THIS FILE CAN AND CANNOT PROVE, because the distinction matters more here
than in most of the suite. The rig in conftest.py is SQLite, which has no roles,
no GRANT and no row-level security — so a test that "checked RLS" here would be
checking nothing at all, and would go green against a live database with every
table wide open. That is precisely the shape of failure this guards against, so
it is worth being explicit: THESE ARE STATEMENT-TEXT ASSERTIONS. They prove the
statements are in the file and that no table this schema creates was left out of
them. They do not prove the file was ever applied.

scripts/check_rls.py is the other half and asks the live database directly.
Neither is sufficient: a database the migration never reached passes this file
and fails that script, and a schema.sql someone trimmed passes that script until
the next fresh install. They fail apart, which is why there are two.
"""

from __future__ import annotations

import re
from pathlib import Path

from scripts.check_rls import evaluate, evaluate_storage, granting_roles
from scripts.init_db import split_manual_block, storage_statements_outside_the_fence

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCHEMA = (PROJECT_ROOT / "db" / "schema.sql").read_text(encoding="utf-8")


def _statements(sql: str) -> str:
    """The file with its `--` comments removed.

    Every assertion below asks what db/schema.sql *executes*, and this file is
    two thirds prose — the comments discuss `CREATE TABLE IF NOT EXISTS` and
    `ALL TABLES IN SCHEMA public` by name, at length, precisely because those
    are the things worth explaining. Matching against the raw text finds those
    sentences: the first draft of this file scraped two phantom tables called
    `never` and `resolves` out of the users block's own explanation of why the
    schema is qualified. A test that reads the commentary is a test that fails
    when someone improves a comment and passes when they delete a statement,
    which is exactly backwards.
    """
    return "\n".join(line.split("--")[0] for line in sql.splitlines())


STATEMENTS = _statements(SCHEMA)

# The two Supabase roles an anon key can act as. service_role is deliberately
# absent: it is the key that is never published and never reaches Vercel, so
# revoking its access would break nothing today and buy nothing either.
EXPOSED_ROLES = ("anon", "authenticated")

# The Phase-1d/1g leftovers. They are not created by this schema, they already
# have RLS on, and Phase 2a was told to leave them alone — so they are excluded
# from the "every table this file creates" sweep rather than silently missing.
NOT_OURS = {"jobs_notified_backup_1g", "jobs_notified_backup_pre1d"}


def tables_this_schema_creates() -> set[str]:
    """Every public table db/schema.sql brings into existence.

    Derived from the file rather than listed here on purpose. A hand-written
    list is a list somebody has to remember to extend, and the failure it would
    hide — a Phase-3 table created without RLS, inheriting Supabase's default
    grants to anon — is the exact failure this file exists to catch.
    """
    found = set(re.findall(r"CREATE TABLE IF NOT EXISTS\s+(?:public\.)?(\w+)", STATEMENTS))
    return found - NOT_OURS


def sequences_this_schema_creates() -> set[str]:
    """Every sequence db/schema.sql brings into existence, derived not listed.

    Two ways a sequence appears in this file and both count. Most tables say
    `id SERIAL PRIMARY KEY`, which is sugar for a sequence named
    `<table>_id_seq`; `preferences` says the three statements SERIAL is sugar
    FOR, spelled out, because 1e had to restore a sequence to a table that had
    been capped at one row and each statement had to stay independently
    idempotent. A test that knew only about the sugar would silently skip the
    one table whose sequence was written by hand.
    """
    found: set[str] = set()

    # Walk the CREATE TABLE blocks in order, so a SERIAL is attributed to the
    # table it is actually inside rather than to whichever name is nearest.
    blocks = re.split(r"CREATE TABLE IF NOT EXISTS\s+(?:public\.)?(\w+)", STATEMENTS)
    for name, body in zip(blocks[1::2], blocks[2::2]):
        if name in NOT_OURS:
            continue
        head = body.split(";", 1)[0]  # the column list only
        if re.search(r"\bSERIAL\b", head, re.IGNORECASE):
            found.add(f"{name}_id_seq")

    found |= set(re.findall(r"CREATE SEQUENCE IF NOT EXISTS\s+(?:public\.)?(\w+)", STATEMENTS))
    return found


def test_schema_creates_the_sequences_we_think_it_does():
    """A sanity check on the derivation above, so the sweep cannot pass vacuously.

    Six is the live count measured on 2026-08-04. public.users and public.jobs
    contribute none — a uuid and a text canonical_id, neither of which needs a
    sequence.
    """
    sequences = sequences_this_schema_creates()
    assert "preferences_id_seq" in sequences, "the hand-written CREATE SEQUENCE was missed"
    assert "companies_id_seq" in sequences, "a plain SERIAL was missed"
    assert "resumes_id_seq" in sequences, "the Slice 1 table's SERIAL was missed"
    assert not any(s.startswith(("users_", "jobs_")) for s in sequences), (
        f"users and jobs have no sequence, so something is over-matching: {sorted(sequences)}"
    )
    # Six through Phase 2; seven since Phase 3 Slice 1 added `resumes`. The count
    # is asserted rather than the membership so that a table added without a
    # thought about its sequence fails here first.
    assert len(sequences) == 7, f"expected 7 sequences, derived {sorted(sequences)}"


def test_schema_creates_the_tables_we_think_it_does():
    """A sanity check on the regex above, so the two sweeps below cannot pass vacuously.

    If CREATE TABLE is ever written differently and this stops matching, every
    other test here would iterate an empty set and report success while proving
    nothing. Eight tables is the Phase-1 count.
    """
    tables = tables_this_schema_creates()
    assert "users" in tables, "public.users must be discoverable — the identity table"
    assert "jobs" in tables
    assert "resumes" in tables, "the Slice 1 table must be swept like every other"
    assert len(tables) >= 9, (
        f"expected at least the 8 Phase-1 tables plus resumes, found {sorted(tables)}"
    )


def test_every_table_enables_row_level_security():
    """No table this schema creates is left with RLS off."""
    missing = [
        table
        for table in sorted(tables_this_schema_creates())
        if not re.search(
            rf"ALTER TABLE\s+public\.{table}\s+ENABLE ROW LEVEL SECURITY\s*;",
            STATEMENTS,
        )
    ]
    assert not missing, (
        "db/schema.sql creates these tables but never enables RLS on them, so the "
        f"published anon key would reach them: {', '.join(missing)}"
    )


def test_every_table_revokes_the_exposed_roles():
    """RLS is the lock; REVOKE is the second lock. Both, on every table."""
    missing = [
        table
        for table in sorted(tables_this_schema_creates())
        if not re.search(
            rf"REVOKE ALL PRIVILEGES ON\s+public\.{table}\s+FROM\s+anon,\s*authenticated\s*;",
            STATEMENTS,
        )
    ]
    assert not missing, (
        "these tables never REVOKE from anon/authenticated, so the privileges "
        f"Supabase grants by default are still held: {', '.join(missing)}"
    )


def test_every_sequence_revokes_the_exposed_roles():
    """The sequences behind the SERIAL columns are revoked too.

    Not tidiness. The live grant was anon=rwU, and the w on a sequence is
    setval — an anon caller could have reset companies_id_seq and made the next
    insert collide on the primary key. RLS does not cover this: row-level
    security is a property of tables, and a sequence has no rows.
    """
    missing = [
        seq
        for seq in sorted(sequences_this_schema_creates())
        if not re.search(
            rf"REVOKE ALL PRIVILEGES ON SEQUENCE\s+public\.{seq}\s+FROM\s+anon,\s*authenticated\s*;",
            STATEMENTS,
        )
    ]
    assert not missing, (
        "these sequences are created but never revoked, so anon keeps rwU — "
        f"which includes setval: {', '.join(missing)}"
    )


def test_default_privileges_are_revoked_for_every_object_type():
    """The NEXT object created in public arrives closed, not open.

    This is the statement that covers Phase 3. Locks 1 and 2 describe the
    objects that exist today; without this one, a table created next month
    inherits Supabase's default grants and is open from the moment it exists —
    and the sweeps in this file would only notice afterwards. A tripwire that
    fires after the hole opens is worth having; a hole that never opens is
    worth more.

    All three object types, including FUNCTIONS, which public currently has
    none of: a function is how PostgREST exposes RPC, and covering it in
    advance costs one line.
    """
    missing = [
        objtype
        for objtype in ("TABLES", "SEQUENCES", "FUNCTIONS")
        if not re.search(
            rf"ALTER DEFAULT PRIVILEGES IN SCHEMA public\s+REVOKE ALL ON {objtype}\s+"
            rf"FROM\s+anon,\s*authenticated\s*;",
            STATEMENTS,
        )
    ]
    assert not missing, (
        "db/schema.sql does not revoke default privileges for: "
        f"{', '.join(missing)} — so the next object of that kind created in "
        "public arrives with Supabase's default grants to anon."
    )


def test_default_privileges_are_not_scoped_to_another_role():
    """No FOR ROLE clause, so the statement binds whoever applies the file.

    ALTER DEFAULT PRIVILEGES affects only objects created by the role that runs
    it. Bare, it means the current role — which is postgres, the role both DSNs
    use and the one pg_default_acl already records as the grantor of the
    entries being revoked. A `FOR ROLE someone_else` would apply to a role that
    creates nothing here, and would be a statement that reads as protection
    while doing nothing at all. scripts/check_rls.py prints set_by for exactly
    this reason.
    """
    scoped = re.findall(r"ALTER DEFAULT PRIVILEGES\s+FOR ROLE\s+(\w+)", STATEMENTS)
    assert not scoped, (
        f"ALTER DEFAULT PRIVILEGES is scoped to role(s) {', '.join(scoped)}, which is "
        "not necessarily the role that applies this schema — drop the FOR ROLE clause"
    )


# Every CREATE POLICY in the file, as (name, table). The table decides which of
# the two rules below judges it; the name is what the file itself declares.
POLICIES = re.findall(r"CREATE POLICY\s+(\w+)\s+ON\s+([\w.]+)", STATEMENTS)

# What the Slice 1 storage block is supposed to declare.
#
# NAMES ARE ASSERTED HERE AND NOWHERE ELSE, and the asymmetry is deliberate. This
# is a test about db/schema.sql, which is the source of truth for a fresh install
# and writes clean names — so it can and should say what those names are.
# scripts/check_rls.py asks the LIVE database, where two of the policies carry a
# suffix the Supabase UI appended, and matches on properties instead: operation,
# role, owner predicate. A name is a fact about this file; it is not a fact about
# whether a resume is reachable.
EXPECTED_STORAGE_POLICIES = {
    "resumes_owner_select": "SELECT",
    "resumes_owner_insert": "INSERT",
    "resumes_owner_update": "UPDATE",
    "resumes_owner_delete": "DELETE",
}


def test_no_policy_is_defined_on_any_public_table():
    """Deny-all is still the whole design in `public`, and a policy would widen it.

    THIS TEST USED TO FORBID CREATE POLICY OUTRIGHT, and its own docstring said
    that failing it was "a prompt to write down why" rather than necessarily a
    bug. Phase 3 Slice 1 is that prompt being taken up: it adds four policies, all
    of them on storage.objects, none on any table in `public`.

    The rule therefore narrowed rather than relaxed. In `public`, RLS with zero
    policies denies every role that does not bypass it, both runtimes connect as
    postgres and bypass it, and nothing needs a policy — so a policy there is
    still somebody widening deny-all and still has to be argued for. In `storage`
    the situation is inverted (see test_the_storage_lock_creates_four_scoped_policies),
    which is exactly why the two are judged by two rules instead of one.
    """
    public_policies = [
        name for name, table in POLICIES if not table.startswith("storage.")
    ]
    assert not public_policies, (
        "db/schema.sql now defines policies on tables in public, which widens "
        f"deny-all: {', '.join(public_policies)}. If that is intended, say so here "
        "and in the schema comment; if it is not, the anon key can now read something."
    )


def test_the_storage_lock_creates_four_scoped_policies():
    """The inverse rule: in `storage`, the policies ARE the security boundary.

    This app has no service-role key, so every read and write of a resume travels
    on the signed-in person's own JWT. There is no privileged path behind these
    policies to fall back on — a missing one is not a smaller permission, it is a
    person unable to reach their own file, and a wrong one is everybody reaching
    everybody's.

    Four things are asserted per policy, and each has a distinct failure:
      - it exists and covers the verb its name claims
      - TO authenticated, and never anon (the anon key is published)
      - the predicate mentions auth.uid(), which is the isolation itself — a
        policy that lost that clause would still be four correctly-named policies
        and would let every signed-in user read every resume
      - UPDATE additionally carries WITH CHECK, without which an owner can rename
        their object into someone else's folder

    Statement-text only, like everything in this file. scripts/check_rls.py asks
    the live database the same four questions.
    """
    storage_policies = {name: table for name, table in POLICIES if table.startswith("storage.")}

    missing = sorted(set(EXPECTED_STORAGE_POLICIES) - set(storage_policies))
    assert not missing, (
        f"the storage lock in db/schema.sql does not create: {', '.join(missing)} — "
        "resume upload or download is denied even to the person who owns the file"
    )
    wrong_table = {n: t for n, t in storage_policies.items() if t != "storage.objects"}
    assert not wrong_table, f"storage policies must be on storage.objects: {wrong_table}"

    for name, cmd in sorted(EXPECTED_STORAGE_POLICIES.items()):
        # The policy body: from its CREATE up to the terminating semicolon.
        body = re.search(rf"CREATE POLICY\s+{name}\s+ON\s+storage\.objects(.*?);", STATEMENTS, re.S)
        assert body, f"could not read the body of policy {name}"
        text = body.group(1)

        assert re.search(rf"\bFOR\s+{cmd}\b", text), (
            f"policy {name} does not say FOR {cmd}, so it covers the wrong verb"
        )
        assert re.search(r"\bTO\s+authenticated\b", text), (
            f"policy {name} is not scoped TO authenticated"
        )
        assert not re.search(r"\bTO\s+[\w\s,]*\banon\b", text), (
            f"policy {name} is scoped to anon — the key in the browser bundle can use it"
        )
        assert "auth.uid()" in text, (
            f"policy {name} does not mention auth.uid(), so it scopes nothing to an "
            f"owner and every authenticated user can reach every resume"
        )
        assert "bucket_id = 'resumes'" in text, (
            f"policy {name} does not restrict itself to the resumes bucket"
        )
        if cmd == "UPDATE":
            assert "WITH CHECK" in text, (
                "resumes_owner_update has no WITH CHECK: USING decides which rows may "
                "be updated, WITH CHECK decides what they may become, and without it an "
                "owner can rename their object into another person's folder"
            )


# --- the fence: what init_db.py is allowed to execute ----------------------
#
# db/schema.sql describes the whole database, including a storage section this
# project's role cannot apply — storage.objects is owned by
# supabase_storage_admin, and on 2026-08-06 attempting it raised 42501 and, because
# asyncpg sends the file as one transaction, rolled back the `resumes` table with
# it. The statements stay in the file as the source of truth and are fenced off
# so the apply path skips them.
#
# These tests are about that fence, and they read the RAW file rather than
# STATEMENTS: the markers are `--` comments, so the comment-stripping helper the
# rest of this file uses would erase the very thing under test.


def test_the_manual_block_is_well_formed():
    """One fence, opened and closed, with the storage statements inside it."""
    executable, manual = split_manual_block(SCHEMA)

    assert "CREATE POLICY" in manual, "the storage policies are not inside the fence"
    assert "storage.buckets" in manual, "the bucket INSERT is not inside the fence"
    assert "ALTER TABLE storage.objects" in manual, (
        "the storage.objects RLS statement is not inside the fence"
    )
    # And the part that DOES get applied still carries the ordinary schema.
    assert "CREATE TABLE IF NOT EXISTS resumes" in executable, (
        "the resumes TABLE has been fenced off along with the storage DDL. It is an "
        "ordinary table in `public` that this project owns and must still be applied."
    )
    assert "ALTER TABLE public.resumes ENABLE ROW LEVEL SECURITY" in executable
    assert "REVOKE ALL PRIVILEGES ON public.resumes" in executable


def test_no_storage_statement_escapes_the_fence():
    """THE GUARD, ASSERTED RATHER THAN TRUSTED.

    The fence only protects anything while everything privileged is inside it,
    and the natural way to add a fifth policy next year is to type it next to the
    fourth — outside the markers, because markers are easy to miss. init_db.py
    refuses to apply the file in that case; this proves the refusal is currently
    not needed, and would catch the day it becomes needed.
    """
    executable, _ = split_manual_block(SCHEMA)
    stray = storage_statements_outside_the_fence(executable)
    assert not stray, (
        "these statements touch Supabase's `storage` schema but sit outside the "
        "MANUAL-ONLY fence, so init_db.py would send them and get 42501 — which "
        "rolls back the whole migration:\n  " + "\n  ".join(stray)
    )


def test_the_guard_actually_fires_on_a_stray_statement():
    """The mutation, because a guard that cannot fail is not a guard.

    test_no_storage_statement_escapes_the_fence passes on an empty result, and it
    would pass just as happily if the detector were broken. This hands it the
    shape it exists to catch.
    """
    stray = storage_statements_outside_the_fence(
        "CREATE TABLE fine (id INT);\nALTER TABLE storage.objects ENABLE ROW LEVEL SECURITY;"
    )
    assert stray == ["ALTER TABLE storage.objects ENABLE ROW LEVEL SECURITY;"], stray

    # And it reads statements rather than prose: this file's own commentary says
    # "storage." constantly, and a guard that matched comments would make the
    # schema fail for explaining itself.
    assert storage_statements_outside_the_fence(
        "-- we cannot touch storage.objects here, see the note\nCREATE TABLE fine (id INT);"
    ) == []


def test_a_malformed_fence_is_refused_rather_than_guessed_at():
    """A missing END marker would silently swallow the rest of the file.

    On this path that means silently not creating tables, and reporting success —
    so the splitter raises instead of returning something plausible.
    """
    import pytest

    with pytest.raises(ValueError, match="never closes it"):
        split_manual_block("CREATE TABLE a (id INT);\n-- >>> MANUAL-ONLY (storage)\nDROP TABLE a;")

    with pytest.raises(ValueError, match="no .* marker"):
        split_manual_block("CREATE TABLE a (id INT);")

    with pytest.raises(ValueError, match="more than one"):
        split_manual_block(
            "-- >>> MANUAL-ONLY (storage)\nA;\n-- <<< END MANUAL-ONLY\n"
            "-- >>> MANUAL-ONLY (storage)\nB;\n-- <<< END MANUAL-ONLY\n"
        )


def test_the_resume_bucket_is_created_private():
    """A public bucket makes all four policies decorative, so it is asserted here too.

    storage.buckets.public = TRUE serves every object over a permanent URL with
    no policy consulted at all. That failure leaves every other assertion in this
    file green, which is precisely why it gets its own line.
    """
    assert re.search(r"INSERT INTO storage\.buckets", STATEMENTS), (
        "db/schema.sql does not create the resumes bucket"
    )
    insert = re.search(r"INSERT INTO storage\.buckets(.*?);", STATEMENTS, re.S)
    assert insert, "could not read the bucket INSERT"
    text = insert.group(1)
    assert "'resumes'" in text
    assert re.search(r"\bFALSE\b", text), "the bucket must be created private (public = FALSE)"
    # The re-apply path matters as much as the create: flipping the bucket public
    # in the Supabase console is one click and nothing in this project would fail.
    assert re.search(r"ON CONFLICT\s*\(\s*id\s*\)\s*DO UPDATE", text), (
        "the bucket INSERT must DO UPDATE, so re-applying the schema closes a bucket "
        "somebody made public in the console. DO NOTHING would leave it open."
    )
    assert re.search(r"SET\s+public\s*=\s*FALSE", text), (
        "the DO UPDATE must set public = FALSE — that is the whole point of it"
    )


def test_the_backup_tables_are_not_touched():
    """Phase 2a was told to leave the Phase-1 backups alone, and does."""
    for table in NOT_OURS:
        assert not re.search(rf"ALTER TABLE\s+public\.{table}\b", STATEMENTS), (
            f"public.{table} is a Phase-1 leftover and not 2a's business"
        )


def test_the_block_does_not_use_all_tables_in_schema():
    """Enumerated, not blanket — so the backups are not silently adopted.

    `REVOKE ... ON ALL TABLES IN SCHEMA public` would be shorter and would also
    sweep in the two backup tables and anything else that happens to be sitting
    in the schema, which is the opposite of the explicitness the rest of this
    file is written with.
    """
    assert "ALL TABLES IN SCHEMA public" not in STATEMENTS, (
        "the RLS block should enumerate tables rather than sweep the schema"
    )


# --- the verdict itself ---------------------------------------------------
#
# Everything above reads db/schema.sql. Everything below exercises
# scripts/check_rls.evaluate(), which is the thing that decides whether the live
# database passes — and which was WRONG once already: it failed on
# supabase_admin's default ACL, a condition postgres cannot revoke, making the
# check permanently red for something nobody could fix. A check that cannot go
# green is a check that gets ignored.
#
# evaluate() is pure precisely so these can exist. The condition property 5
# guards — a supabase_admin-OWNED table in public — does not exist on the real
# database and cannot be created there to test against, so the only way to know
# the criterion bites is to hand it that shape directly.


# The live shape as measured on 2026-08-04, plus `resumes` from Phase 3 Slice 1.
LIVE_TABLES = [
    "companies", "discovered_companies", "jobs", "notifications",
    "preferences", "resumes", "user_companies", "user_job_notifications", "users",
]


def _facts(**overrides):
    """The post-migration live shape, as evaluate() takes it. Override to mutate."""
    facts = {
        "connecting_role": "postgres",
        "bypassrls": True,
        "rls_rows": [
            {"tablename": t, "rowsecurity": True} for t in LIVE_TABLES + sorted(NOT_OURS)
        ],
        # The 28 real grants, all of them on the excluded backups.
        "grant_rows": [
            {"grantee": role, "table_name": t, "privilege_type": priv}
            for t in sorted(NOT_OURS)
            for role in EXPOSED_ROLES
            for priv in ("SELECT", "INSERT", "UPDATE", "DELETE",
                         "TRUNCATE", "REFERENCES", "TRIGGER")
        ],
        "sequence_rows": [
            {"name": f"{t}_id_seq", "acl": ["postgres=rwU/postgres"]}
            for t in ("companies", "discovered_companies", "notifications",
                      "preferences", "resumes", "user_companies",
                      "user_job_notifications")
        ],
        "default_acls": [
            # ours, cleared by the migration
            {"objtype": "r", "set_by": "postgres", "acl": ["postgres=arwdDxtm/postgres"]},
            {"objtype": "S", "set_by": "postgres", "acl": ["postgres=rwU/postgres"]},
            {"objtype": "f", "set_by": "postgres", "acl": ["postgres=X/postgres"]},
            # Supabase's own, which postgres can neither revoke nor create under
            {"objtype": "r", "set_by": "supabase_admin",
             "acl": ["anon=arwdDxtm/supabase_admin", "authenticated=arwdDxtm/supabase_admin"]},
            {"objtype": "S", "set_by": "supabase_admin",
             "acl": ["anon=rwU/supabase_admin", "authenticated=rwU/supabase_admin"]},
            {"objtype": "f", "set_by": "supabase_admin",
             "acl": ["anon=X/supabase_admin", "authenticated=X/supabase_admin"]},
        ],
        "table_owners": [
            {"name": t, "owner": "postgres"} for t in LIVE_TABLES + sorted(NOT_OURS)
        ],
    }
    facts.update(overrides)
    return facts


def test_the_live_shape_passes():
    """The database as it stands is green — including the inert foreign entries.

    This is the assertion the earlier version of the check failed, and the whole
    reason it had to be narrowed: supabase_admin's default ACL is real, is
    un-revokable by postgres, and opens nothing.
    """
    failures, notes = evaluate(**_facts())
    assert failures == [], f"the live shape should pass, got: {failures}"
    assert any("supabase_admin" in n and "INERT" in n for n in notes), (
        f"the foreign default ACL must be REPORTED rather than silently dropped: {notes}"
    )


def test_a_supabase_admin_owned_table_flips_it_red():
    """Property 5, the future-guard, and the whole reason evaluate() is pure.

    A default ACL governs only objects created by the role that set it. So
    supabase_admin's entry is inert exactly while no table in public is owned by
    supabase_admin — and the moment one is, that table arrives with anon holding
    full DML. This is the mutation the criterion exists for, and it cannot be
    staged on the real database.
    """
    facts = _facts()
    facts["table_owners"] = facts["table_owners"] + [
        {"name": "some_new_table", "owner": "supabase_admin"}
    ]
    facts["rls_rows"] = facts["rls_rows"] + [
        {"tablename": "some_new_table", "rowsecurity": True}
    ]

    failures, _ = evaluate(**facts)
    assert any("some_new_table" in f and "supabase_admin" in f for f in failures), (
        f"a supabase_admin-owned public table must fail property 5, got: {failures}"
    )


def test_the_connecting_roles_own_defaults_still_fail():
    """Property 4 — the half we control, and the half the migration fixed.

    Narrowing the check must not have narrowed away the thing it was for. If
    postgres's own default ACL grants anon again, the next table THIS project
    creates arrives open, and that is a failure regardless of ownership.
    """
    facts = _facts()
    facts["default_acls"] = [
        {"objtype": "r", "set_by": "postgres",
         "acl": ["postgres=arwdDxtm/postgres", "anon=arwdDxtm/postgres"]}
    ] + facts["default_acls"][1:]

    failures, _ = evaluate(**facts)
    assert any("THIS project creates" in f for f in failures), (
        f"the connecting role's own default ACL must still fail, got: {failures}"
    )


def test_rls_off_on_an_owned_table_fails():
    """Property 1 survived the rewrite."""
    facts = _facts()
    facts["rls_rows"] = [
        {**r, "rowsecurity": r["tablename"] != "jobs"} for r in facts["rls_rows"]
    ]
    failures, _ = evaluate(**facts)
    assert any("public.jobs" in f for f in failures), failures


def test_a_grant_on_an_owned_table_fails():
    """Property 2 survived the rewrite — and the backups are still excluded."""
    facts = _facts()
    facts["grant_rows"] = facts["grant_rows"] + [
        {"grantee": "anon", "table_name": "jobs", "privilege_type": "SELECT"}
    ]
    failures, _ = evaluate(**facts)
    assert any("jobs" in f and "table grant" in f for f in failures), failures


def test_a_sequence_grant_fails():
    """Property 3 survived the rewrite."""
    facts = _facts()
    facts["sequence_rows"] = [
        {"name": "companies_id_seq", "acl": ["anon=rwU/postgres"]}
    ] + facts["sequence_rows"][1:]
    failures, _ = evaluate(**facts)
    assert any("companies_id_seq" in f for f in failures), failures


def test_the_backups_stop_being_excused_if_rls_comes_off():
    """The exclusion is conditional on RLS, not on the table's name.

    The two Phase-1 backups keep their grants and are forgiven for it because
    RLS denies everyone. That forgiveness must not be a hardcoded pass — turn
    RLS off on one and the grants it carries become live.
    """
    facts = _facts()
    facts["rls_rows"] = [
        {**r, "rowsecurity": r["tablename"] != "jobs_notified_backup_1g"}
        for r in facts["rls_rows"]
    ]
    failures, _ = evaluate(**facts)
    assert any("jobs_notified_backup_1g" in f and "RLS OFF" in f for f in failures), failures


def test_a_non_bypassrls_connection_fails_loudly():
    """If the connecting role does not bypass RLS, we locked ourselves out."""
    failures, _ = evaluate(**_facts(bypassrls=False, connecting_role="some_other_role"))
    assert any("NOT bypassrls" in f for f in failures), failures


# --- property 6, the storage half ------------------------------------------
#
# evaluate_storage() judges a PRESENCE where the five above judge an absence, so
# the mutations are the other way round too: the failures worth staging are a
# policy that went MISSING or that got WIDER, not one that appeared.


def _bucket(**overrides):
    """The intended live shape of the bucket: exists, private, RLS on."""
    row = {"exists": True, "is_public": False, "objects_rls": True}
    row.update(overrides)
    return row


# The owner predicate, spelled the way pg_policies DEPARSES it rather than the
# way db/schema.sql writes it — `bucket_id = 'resumes'` comes back as
# `(bucket_id = 'resumes'::text)` and `auth.uid()::text` as `((auth.uid())::text)`.
# That rewritten form is what evaluate_storage actually reads, so a fixture using
# the source spelling would be testing against text the database never produces.
OWNER_QUAL = (
    "((bucket_id = 'resumes'::text) AND "
    "((storage.foldername(name))[1] = (auth.uid())::text))"
)


def _named_policies():
    """The four policies as db/schema.sql describes them, keyed by operation."""
    return {
        "select": {
            "policyname": "resumes_owner_select", "cmd": "SELECT",
            "roles": "{authenticated}", "qual": OWNER_QUAL, "with_check": None,
        },
        "insert": {
            "policyname": "resumes_owner_insert", "cmd": "INSERT",
            "roles": "{authenticated}", "qual": None, "with_check": OWNER_QUAL,
        },
        "update": {
            "policyname": "resumes_owner_update", "cmd": "UPDATE",
            "roles": "{authenticated}", "qual": OWNER_QUAL, "with_check": OWNER_QUAL,
        },
        "delete": {
            "policyname": "resumes_owner_delete", "cmd": "DELETE",
            "roles": "{authenticated}", "qual": OWNER_QUAL, "with_check": None,
        },
    }


def _storage_policies(**overrides):
    """The live rows evaluate_storage sees. Override by operation; None removes."""
    rows = _named_policies()
    rows.update(overrides)
    return [rows[k] for k in sorted(rows) if rows[k] is not None]


def test_the_intended_storage_shape_passes():
    """The bucket and policies this schema describes are green."""
    failures, notes = evaluate_storage(_bucket(), _storage_policies())
    assert failures == [], f"the intended storage shape should pass, got: {failures}"
    # The names are REPORTED — that is how the suffixed live ones stay visible
    # without deciding anything.
    assert any("matched by property" in n for n in notes), notes


def test_a_ui_suffixed_policy_name_still_passes():
    """THE REGRESSION THIS PROPERTY MATCHING EXISTS FOR.

    Two of the live policies were created through the Supabase UI, which appends
    a suffix — `resumes_owner_select i5g8va_0`. The first version of this check
    keyed on the name and went red against a database that was completely
    correct, which is the failure a security check can least afford: one that
    cries wolf gets switched off, and this is the check standing between a resume
    and the internet.
    """
    renamed = {}
    for key, policy in _named_policies().items():
        renamed[key] = {**policy, "policyname": f"{policy['policyname']} i5g8va_0"}

    failures, _ = evaluate_storage(_bucket(), _storage_policies(**renamed))
    assert failures == [], f"a suffixed policy name must not fail anything, got: {failures}"


def test_policies_named_anything_at_all_still_pass():
    """The name is not merely tolerated with a suffix — it is not read.

    Stated separately from the suffix case so the rule is unambiguous: what makes
    a policy this project's is that it governs the resumes bucket, and what makes
    it correct is its predicate. A future UI that renames differently, or a
    hand-written policy called something sensible, must not need a code change.
    """
    odd = {}
    for i, (key, policy) in enumerate(_named_policies().items()):
        odd[key] = {**policy, "policyname": f"xyzzy_{i}"}

    failures, _ = evaluate_storage(_bucket(), _storage_policies(**odd))
    assert failures == [], f"policy names must not decide anything, got: {failures}"


def test_a_missing_bucket_fails():
    """The migration never ran, or rolled back on the 42501 the schema warns about."""
    failures, _ = evaluate_storage(_bucket(exists=False), [])
    assert any("no row with id = 'resumes'" in f for f in failures), failures


def test_a_public_bucket_fails():
    """The one failure that leaves every other check green.

    A public bucket serves objects over a permanent URL without consulting a
    policy, so all four policies can be perfect and every resume still readable.
    """
    failures, _ = evaluate_storage(_bucket(is_public=True), _storage_policies())
    assert any("PUBLIC" in f for f in failures), failures


def test_rls_off_on_storage_objects_fails():
    """Policies on a table with RLS off are inert — every object is readable."""
    failures, _ = evaluate_storage(_bucket(objects_rls=False), _storage_policies())
    assert any("RLS is OFF on storage.objects" in f for f in failures), failures


def test_no_policy_at_all_fails():
    """The manual block was never pasted. Nobody can reach their own resume."""
    failures, _ = evaluate_storage(_bucket(), [])
    assert any("no policy on storage.objects governs" in f for f in failures), failures


def test_a_missing_policy_fails():
    """Each verb is a separate capability, so each absence is a separate failure."""
    for operation, verb in (("select", "SELECT"), ("insert", "INSERT"),
                            ("update", "UPDATE"), ("delete", "DELETE")):
        failures, _ = evaluate_storage(_bucket(), _storage_policies(**{operation: None}))
        assert any(f"no {verb} policy governs" in f for f in failures), (operation, failures)


def test_a_policy_without_the_owner_predicate_fails():
    """THE MUTATION THAT MATTERS MOST, AND THE ONE A NAME CHECK COULD NEVER SEE.

    This policy is called `resumes_owner_select`, covers SELECT, is scoped to
    authenticated, and sits on a private bucket. Everything a name-and-count
    check would look at is perfect. It has simply lost the folder comparison —
    so every signed-in user can read every resume in the bucket.

    That is the whole argument for matching on properties: the old version of
    this check would have passed this input.
    """
    widened = {
        "policyname": "resumes_owner_select", "cmd": "SELECT",
        "roles": "{authenticated}",
        "qual": "(bucket_id = 'resumes'::text)",  # the folder check, gone
        "with_check": None,
    }
    failures, _ = evaluate_storage(_bucket(), _storage_policies(select=widened))
    assert any("does not scope objects to their owner" in f for f in failures), failures


def test_an_update_policy_without_with_check_fails():
    """USING says which rows you may update; WITH CHECK says what they may become.

    Without it an owner can rename their own object into another person's folder,
    which passes USING on the way in and lands somewhere they cannot be stopped
    from reading.
    """
    no_check = {**_named_policies()["update"], "with_check": None}
    failures, _ = evaluate_storage(_bucket(), _storage_policies(update=no_check))
    assert any("no WITH CHECK" in f for f in failures), failures


def test_an_anon_scoped_policy_fails():
    """The published key acts as anon. No policy on this bucket may name it."""
    anon = {**_named_policies()["select"], "roles": "{anon,authenticated}"}
    failures, _ = evaluate_storage(_bucket(), _storage_policies(select=anon))
    assert any("scoped to anon" in f for f in failures), failures


def test_a_policy_not_scoped_to_authenticated_fails():
    """A policy for PUBLIC covers anon too, without ever naming it.

    `TO public` is the Postgres default when no role is given, and it reads as
    harmless. It is not: `public` includes every role, so the anon key gets the
    policy by inheritance rather than by being mentioned — which is why the role
    is asserted positively instead of only checking that anon is absent.
    """
    everyone = {**_named_policies()["select"], "roles": "{public}"}
    failures, _ = evaluate_storage(_bucket(), _storage_policies(select=everyone))
    assert any("not scoped TO authenticated" in f for f in failures), failures


def test_two_policies_for_one_operation_fail():
    """Policies are OR-ed, so a second one can only widen the first.

    The realistic shape is a debugging policy added to unblock something and
    never removed: the strict one is still there, still correct, and no longer
    deciding anything.
    """
    extra = _storage_policies() + [{
        "policyname": "temp_debug_read", "cmd": "SELECT",
        "roles": "{authenticated}",
        "qual": "(bucket_id = 'resumes'::text)",
        "with_check": None,
    }]
    failures, _ = evaluate_storage(_bucket(), extra)
    assert any("policies cover SELECT" in f for f in failures), failures


def test_an_unrelated_storage_policy_is_reported_not_failed():
    """A policy for some other bucket is somebody else's decision, and visible.

    It is identified as somebody else's by naming a different bucket, not by
    being named differently — which is the same rule that lets the suffixed
    resume policies count as ours.
    """
    extra = _storage_policies() + [{
        "policyname": "avatars_public_read", "cmd": "SELECT",
        "roles": "{anon}", "qual": "(bucket_id = 'avatars'::text)", "with_check": None,
    }]
    failures, notes = evaluate_storage(_bucket(), extra)
    assert failures == [], f"an unrelated bucket's policy is not a failure: {failures}"
    assert any("avatars_public_read" in n for n in notes), notes


def test_granting_roles_ignores_roles_that_grant_nothing():
    """The helper keys on the role that SET the default, and only when it grants.

    postgres appears in pg_default_acl with an entry granting only itself; that
    is not a grant to anon and must not put the role in the mapping, or property
    4 would fail permanently on a database that is entirely correct.
    """
    assert granting_roles([
        {"objtype": "r", "set_by": "postgres", "acl": ["postgres=arwdDxtm/postgres"]},
    ]) == {}
    assert granting_roles([
        {"objtype": "r", "set_by": "supabase_admin", "acl": ["anon=arwdDxtm/supabase_admin"]},
    ]) == {"supabase_admin": ["TABLES"]}
