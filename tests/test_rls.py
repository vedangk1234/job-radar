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

from scripts.check_rls import evaluate, granting_roles

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
    assert not any(s.startswith(("users_", "jobs_")) for s in sequences), (
        f"users and jobs have no sequence, so something is over-matching: {sorted(sequences)}"
    )
    assert len(sequences) == 6, f"expected 6 sequences, derived {sorted(sequences)}"


def test_schema_creates_the_tables_we_think_it_does():
    """A sanity check on the regex above, so the two sweeps below cannot pass vacuously.

    If CREATE TABLE is ever written differently and this stops matching, every
    other test here would iterate an empty set and report success while proving
    nothing. Eight tables is the Phase-1 count.
    """
    tables = tables_this_schema_creates()
    assert "users" in tables, "public.users must be discoverable — the identity table"
    assert "jobs" in tables
    assert len(tables) >= 8, f"expected at least the 8 Phase-1 tables, found {sorted(tables)}"


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


def test_no_policy_is_defined_anywhere():
    """Deny-all is the whole design, and a policy is the only way to widen it.

    RLS with zero policies denies every role that does not bypass it. The moment
    a CREATE POLICY appears, some role can read something, and that is a
    decision that has to be argued rather than inherited from a tutorial. This
    test failing is not necessarily a bug — it is a prompt to write down why.
    """
    policies = re.findall(r"CREATE POLICY\s+(\w+)", STATEMENTS)
    assert not policies, (
        "db/schema.sql now defines policies, which widens deny-all: "
        f"{', '.join(policies)}. If that is intended, say so here and in the "
        "schema comment; if it is not, the anon key can now read something."
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


# The live shape as measured on 2026-08-04, after the migration.
LIVE_TABLES = [
    "companies", "discovered_companies", "jobs", "notifications",
    "preferences", "user_companies", "user_job_notifications", "users",
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
                      "preferences", "user_companies", "user_job_notifications")
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
