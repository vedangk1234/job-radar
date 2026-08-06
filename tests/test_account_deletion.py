"""Phase 2c — sign-out and erasure, asserted rather than demonstrated once.

Deleting an account is the only irreversible operation this project has. Every
other failure mode in the suite is recoverable by re-running something: a missed
job is re-crawled, a wrong filter is edited, a duplicate notification is
suppressed by a unique index. Here there is no second chance on either side —
deleting too much cannot be undone, and deleting too little leaves personal data
behind in a product that told its owner it was gone.

So this file asks three separate questions, in the three places each can be
answered honestly:

    THE SCHEMA      does public.users actually take the per-user tables with it?
                    A property of db/schema.sql, so it is read out of the file.
    THE MIRROR      and does a DELETE of that row behave that way when run?
                    conftest's SQLite mirror with foreign keys switched on.
    THE SOURCE      does the delete remove BOTH records, bind them to the
                    session, and stay inside one transaction? Textual, over the
                    TypeScript, in the same style as tests/test_dashboard_auth.py
                    — with the same caveat: these prove the shapes that make the
                    guarantee possible to reason about. What proves the delete
                    COMMITS, cascades and orphans nothing against real Postgres
                    is `npm run delete-check`, which does it to a throwaway
                    account on the live database.

WHY THE CASCADE IS NOT TESTED BY RUNNING THE PRODUCTION STATEMENT. Everywhere
else in this suite the mirror's whole value is that it runs the real SQL. The
delete's real SQL is TypeScript (dashboard/src/lib/account.ts) and no Python
test can execute it, so a mirror test here runs a re-typed DELETE — which proves
the CASCADE clauses, not the statement. That is still worth having, because the
cascade is the half that lives in db/schema.sql and is owned by this side of the
tree; the statement is checked textually below and executed for real by
delete-check.
"""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCHEMA = PROJECT_ROOT / "db" / "schema.sql"
SRC = PROJECT_ROOT / "dashboard" / "src"

# The second account, matching conftest's USER_B_ID. Spelled here rather than
# imported because the tests below that need one are about a DELETE, not about
# the fan-out that constant was introduced for.
OTHER_USER_ID = "00000000-0000-4000-8000-00000000000b"


def code_lines(text: str) -> list[str]:
    """Lines with `//` comments stripped and block-comment bodies dropped.

    The same helper tests/test_dashboard_auth.py defines, for the same reason:
    every rule below is discussed at length in prose in the files it constrains,
    and matching raw text would fail a file for documenting its own rule.
    """
    out: list[str] = []
    in_block = False
    for raw in text.splitlines():
        line = raw
        if in_block:
            if "*/" in line:
                line = line.split("*/", 1)[1]
                in_block = False
            else:
                continue
        while "/*" in line:
            before, rest = line.split("/*", 1)
            if "*/" in rest:
                line = before + rest.split("*/", 1)[1]
            else:
                line = before
                in_block = True
                break
        line = line.split("//", 1)[0]
        if line.strip():
            out.append(line)
    return out


def source(*parts: str) -> str:
    return SRC.joinpath(*parts).read_text(encoding="utf-8")


def body(*parts: str) -> str:
    """One TypeScript file with its commentary removed."""
    return "\n".join(code_lines(source(*parts)))


# --- the schema: what a deleted account takes with it ---------------------

# The tables that hang off public.users. Named by hand rather than discovered,
# so a per-user table added next month fails this list and has to be considered —
# which is the moment to decide whether it holds personal data, not the moment
# somebody asks for their data back.
#
# `resumes` arrived in Phase 3 Slice 1 and is the first entry here whose deletion
# is NOT fully described by this list. The row cascades like the other three; the
# FILE it names is an object in a Supabase Storage bucket, which no foreign key
# reaches. That half is dashboard/src/lib/account.ts's job and is asserted
# separately below — see the storage-erasure tests.
PER_USER_TABLES = ["preferences", "resumes", "user_companies", "user_job_notifications"]


_CREATE = re.compile(r"^\s*CREATE TABLE(?:\s+IF NOT EXISTS)?\s+(?:public\.)?(\w+)", re.IGNORECASE)
_ALTER = re.compile(r"^\s*ALTER TABLE\s+(?:public\.)?(\w+)", re.IGNORECASE)
_REFERENCES = re.compile(r"REFERENCES\s+public\.users\s*\(\s*id\s*\)", re.IGNORECASE)


def account_foreign_keys() -> list[tuple[str, str]]:
    """Every (table, line) in db/schema.sql that binds a column to public.users.

    One forward pass tracking the statement in progress, rather than anything
    cleverer. db/schema.sql explains itself at length and its prose says
    "REFERENCES public.users" more than once, so comment lines are dropped first
    — matching them would make the file's own documentation fail the test, which
    is the same trap code_lines() exists for on the TypeScript side.

    Both declaration styles are covered because the schema uses both:
    user_companies and user_job_notifications declare the column inline in
    CREATE TABLE, while preferences gained it later through ALTER TABLE.
    """
    found: list[tuple[str, str]] = []
    table = "<none>"
    for raw in SCHEMA.read_text(encoding="utf-8").splitlines():
        line = raw.split("--", 1)[0]
        if not line.strip():
            continue
        create = _CREATE.match(line)
        alter = _ALTER.match(line)
        if create:
            table = create.group(1)
        elif alter:
            table = alter.group(1)
        if _REFERENCES.search(line):
            found.append((table, line.strip()))
    return found


def test_every_per_user_foreign_key_cascades_from_the_account_row():
    """Erasure is one DELETE, and this is what makes that true.

    Without ON DELETE CASCADE the delete does not merely leave data behind — it
    fails outright on the first foreign key, so the failure would at least be
    loud. What this really guards is the half-migrated case: a key re-added later
    without the clause, on one table, discovered by nobody until a person's watch
    list outlives their account.
    """
    keys = account_foreign_keys()
    assert keys, "no foreign key to public.users found at all — this would pass vacuously"

    tables = {table for table, _ in keys}
    assert tables == set(PER_USER_TABLES), (
        f"the set of tables hanging off an account has changed: {sorted(tables)}. "
        f"Every one of them is deleted with the person, so a new entry has to be "
        f"considered here AND in dashboard/src/lib/account.ts."
    )

    uncascaded = [
        f"{table}: {line}" for table, line in keys if "ON DELETE CASCADE" not in line.upper()
    ]
    assert not uncascaded, (
        "these references to public.users do not cascade, so deleting an account "
        "would strand their rows:\n  " + "\n  ".join(uncascaded)
    )


def test_the_shared_catalogue_does_not_hang_off_the_account():
    """A person leaving takes their data and nothing of anybody else's.

    companies and jobs are the shared crawl — one board fetch an hour for
    everybody who watches it — and `notifications` is a job-level sent-log with
    no user column at all. None of the three carries personal data, and none may
    acquire a cascading link to an account: if one did, a single deletion would
    take a company out from under every other person watching it.
    """
    shared = {"companies", "jobs", "notifications", "discovered_companies"}
    offenders = [f"{table}: {line}" for table, line in account_foreign_keys() if table in shared]
    assert not offenders, (
        "shared, non-personal tables reference public.users and would be cascaded "
        "into by a deletion:\n  " + "\n  ".join(offenders)
    )


# --- the mirror: the cascade, executed ------------------------------------


async def test_deleting_the_account_row_removes_every_per_user_table(conn):
    """Zero rows left for that user, in every table that had some.

    The arrangement gives the account one of everything — a filter, a watch list,
    a notification history, a resume reference — because "the cascade works" is
    one claim per table and a test that only had preferences would pass while
    user_companies leaked.
    """
    conn.enforce_foreign_keys()
    await conn.add_company(1, "Company 1")
    await conn.add_job("c1-a", company_id=1, title="Backend Engineer")
    await conn.add_job("c1-b", company_id=1, title="Data Engineer")

    user = await conn.add_user()
    await conn.add_preferences_for(user)
    await conn.track_company(user, 1)
    await conn.notify_user(user, "c1-a")
    await conn.notify_user(user, "c1-b")
    await conn.add_resume_for(user)

    assert await conn.fetchval("SELECT count(*) FROM preferences WHERE user_id = $1", user) == 1
    assert await conn.fetchval("SELECT count(*) FROM user_companies WHERE user_id = $1", user) == 1
    assert await conn.fetchval("SELECT count(*) FROM resumes WHERE user_id = $1", user) == 1
    assert (
        await conn.fetchval(
            "SELECT count(*) FROM user_job_notifications WHERE user_id = $1", user
        )
        == 2
    )

    await conn.execute("DELETE FROM users WHERE id = $1", user)

    assert await conn.fetchval("SELECT count(*) FROM users WHERE id = $1", user) == 0
    for table in PER_USER_TABLES:
        left = await conn.fetchval(f"SELECT count(*) FROM {table} WHERE user_id = $1", user)
        assert left == 0, f"{left} row(s) of {table} outlived the account"


async def test_deleting_one_account_leaves_the_other_untouched(conn):
    """The property the live check restates about a real account with real rows.

    Isolation is asserted here the way tests/test_leakage.py asserts it for the
    fan-out — both users given identical data, so nothing can pass because one of
    them happened to own less.
    """
    conn.enforce_foreign_keys()
    await conn.add_company(1, "Company 1")
    await conn.add_job("c1-a", company_id=1, title="Backend Engineer")

    a = await conn.add_user()
    b = await conn.add_user(OTHER_USER_ID, "second@example.test")
    for user in (a, b):
        await conn.add_preferences_for(user)
        await conn.track_company(user, 1)
        await conn.notify_user(user, "c1-a")
        await conn.add_resume_for(user)

    await conn.execute("DELETE FROM users WHERE id = $1", a)

    assert await conn.tracking_rows() == {(b, 1)}
    assert await conn.notification_pairs() == {(b, "c1-a")}
    assert await conn.fetchval("SELECT count(*) FROM preferences WHERE user_id = $1", b) == 1
    # B's resume reference is not a casualty of A leaving, and neither is the
    # object it points at — nothing here names B's bucket path but A's delete.
    assert await conn.fetchval("SELECT count(*) FROM resumes WHERE user_id = $1", b) == 1
    assert await conn.fetchval("SELECT count(*) FROM resumes WHERE user_id = $1", a) == 0
    # and the shared catalogue is not a casualty of somebody leaving
    assert await conn.fetchval("SELECT count(*) FROM companies") == 1
    assert await conn.fetchval("SELECT count(*) FROM jobs") == 1


async def test_the_account_row_can_carry_an_auth_id_that_differs_from_its_own(conn):
    """The Phase-1 asymmetry, which is why the delete needs two ids and not one.

    Every account created since 2a has public.users.id = auth.users.id, and code
    written against only those accounts would look correct while deleting the
    identity by the wrong id. The one row where they differ is the operator's, so
    the failure would appear exactly once, on the only account that matters, and
    would leave an orphaned Supabase identity behind reporting success. The
    mirror carries the column so this stays a stated property rather than a
    remembered one.
    """
    user = await conn.add_user()
    await conn.execute(
        "UPDATE users SET auth_user_id = $1 WHERE id = $2", "an-entirely-different-uuid", user
    )
    stored = await conn.fetchval("SELECT auth_user_id FROM users WHERE id = $1", user)
    assert stored != user, (
        "the mirror cannot represent an account whose auth identity differs from "
        "its own id, so no test here could catch a delete that used the wrong one"
    )


# --- the source: what the delete may and may not do -----------------------


def test_the_delete_removes_both_records():
    """public.users AND auth.users. Either alone is worse than neither.

    Only the app row: the identity survives, so the next request from a live
    session walks into link-user branch 3 and creates a fresh account — the
    person is not deleted, they are reset. Only the identity: every row of theirs
    survives, including the email address, which is the one column the right to
    erasure is actually about.
    """
    account = body("lib", "account.ts")
    assert re.search(r"DELETE FROM public\.users", account), (
        "lib/account.ts does not delete the public.users row"
    )
    assert re.search(r"DELETE FROM auth\.users", account), (
        "lib/account.ts does not delete the auth.users identity — the app data "
        "would go and the Supabase account would remain"
    )


def test_both_deletes_are_in_one_transaction():
    """The orphan is not handled, it is made unreachable.

    Two statements that can commit apart have a state between them: the account
    erased and the identity still live, or the reverse. One transaction has no
    such state — the only way to have "handled" it otherwise is a repair job that
    runs later and a window in which the database is wrong.
    """
    account = body("lib", "account.ts")
    assert "sql.begin" in account, (
        "the two deletes are not wrapped in sql.begin(), so they can commit apart "
        "and leave a half-deleted person"
    )
    begin_at = account.index("sql.begin")
    for statement in ("DELETE FROM public.users", "DELETE FROM auth.users"):
        assert account.index(statement) > begin_at, f"{statement} runs outside the transaction"


def test_the_identity_is_deleted_by_the_auth_uid_and_never_by_the_account_id():
    """THE ONE THAT WOULD HAVE NO SYMPTOM.

    public.users.id and auth.users.id are equal for every account created since
    2a, so `DELETE FROM auth.users WHERE id = <userId>` passes every test written
    against a new account, deletes nothing at all for the Phase-1 account, and
    reports success either way.
    """
    account = body("lib", "account.ts")
    match = re.search(r"DELETE FROM auth\.users WHERE id = \$\{(\w+)\}", account)
    assert match, "could not find the auth.users delete predicate in lib/account.ts"
    assert match.group(1) == "authUserId", (
        f"the auth identity is being deleted by `{match.group(1)}`; it must be the "
        f"auth uid from the verified session, because the two ids differ for the "
        f"Phase-1 account"
    )


def test_the_account_delete_is_bound_to_the_authenticated_identity():
    """Both columns in the predicate, so one stolen id is not enough."""
    account = body("lib", "account.ts")
    assert re.search(
        r"DELETE FROM public\.users\s*WHERE id = \$\{userId\}\s*AND auth_user_id = \$\{authUserId\}",
        account,
    ), (
        "the public.users delete must match BOTH the account id and the auth "
        "identity from the session"
    )


def test_the_cascade_is_verified_before_the_transaction_commits():
    """A missing CASCADE must abort the delete, not quietly outlive it."""
    account = body("lib", "account.ts")
    assert "CascadeFailedError" in account, (
        "nothing checks that the per-user rows actually went; a dropped CASCADE "
        "would leave them behind with the account gone and no error anywhere"
    )


def test_nothing_in_the_delete_path_takes_a_user_id_from_the_request():
    """Deleting by session identity only — the 2a rule, where it matters most.

    Everywhere else in this app a trusted id decides what a person may READ. Here
    it decides whose data stops existing, and a userId accepted from a form would
    be an authenticated stranger deleting somebody else's account with a curl
    command.
    """
    actions = body("app", "actions.ts")

    assert "requirePage()" in actions, "the delete action does not resolve a session"

    reads = re.findall(r"formData\.get\(\s*[\"'](\w+)[\"']\s*\)", actions)
    assert reads, "the delete action reads nothing from the form — the confirmation is missing"
    assert set(reads) == {"confirm"}, (
        "the delete action reads more than the typed confirmation from the request: "
        + ", ".join(sorted(set(reads)))
        + ". Identity comes from the session and from nowhere else."
    )

    # And the call itself is made with the session's values, not with anything
    # reconstructed alongside them.
    assert "deleteAccount(userId, authUserId" in actions, (
        "deleteAccount() is not being called with the ids destructured from "
        "requirePage()"
    )


def test_the_confirmation_is_checked_on_the_server():
    """A client-side confirm is absent exactly when the form still posts.

    This dashboard is built to work with JavaScript off — that is why sign-out is
    a server action and why the delete form is a server component. A confirmation
    that lived in the browser would therefore be missing in precisely the
    situation the form still submits in, and present only for people who were
    never the risk.
    """
    actions = body("app", "actions.ts")
    assert re.search(r"formData\.get\(\s*[\"']confirm[\"']\s*\)", actions), (
        "the server action does not read the typed confirmation"
    )
    assert "toLowerCase()" in actions, (
        "the confirmation must be compared case-insensitively, matching the "
        "lower(email) comparison link-user.ts makes"
    )

    component = source("components", "DeleteAccount.tsx")
    assert not component.lstrip().startswith('"use client"'), (
        "the delete form is a client component; it must render and submit with "
        "JavaScript disabled"
    )
    assert "confirm(" not in "\n".join(code_lines(component)), (
        "a window.confirm() is not a confirmation — it is absent with scripting "
        "off and no obstacle to a direct POST"
    )


def test_erasure_needs_no_service_role_key():
    """Decision D, restated as a test now that something could have broken it.

    Deleting an auth user is the documented use for the service-role key, and
    reaching for it here is the obvious thing to do. It would put an
    all-powerful credential on Vercel — undoing the property that lets the anon
    key be published at all — to buy a WEAKER guarantee than the pooler
    transaction gives, because a DELETE followed by an HTTPS admin call is two
    operations that can fail apart.
    """
    offenders = [
        f"{path.relative_to(PROJECT_ROOT)}: {line.strip()}"
        for path in sorted(SRC.rglob("*.ts*"))
        for line in code_lines(path.read_text(encoding="utf-8"))
        if "SERVICE_ROLE" in line or "service_role" in line or "auth.admin" in line
    ]
    assert not offenders, (
        "the app reaches for the service-role key or the admin API:\n  " + "\n  ".join(offenders)
    )


# --- sign-out -------------------------------------------------------------


def test_sign_out_clears_the_session_and_lands_on_signin():
    """Untested since 2a built it, which is the only thing wrong with it.

    Both halves are asserted because they fail apart and only one of them is
    visible: a redirect without signOut() looks completely correct — the browser
    lands on /signin — while the session is still live and the back button walks
    straight back in.
    """
    actions = body("app", "actions.ts")
    match = re.search(
        r"export async function signOutEverywhere\(\)[^}]*?\}", actions, re.DOTALL
    )
    assert match, "signOutEverywhere is gone"
    fn = match.group(0)
    assert "auth.signOut()" in fn, "sign-out does not actually revoke the session"
    assert 'redirect("/signin")' in fn, "sign-out does not send the person to /signin"


def test_sign_out_is_a_server_action_posted_by_a_form():
    """No JavaScript, by construction rather than by luck.

    A first paint on a phone with a bad connection is a page whose JavaScript has
    not arrived, which is exactly the situation this dashboard gets opened in. A
    button with an onClick would be inert there; a form with a server action
    posts.
    """
    nav = source("components", "Nav.tsx")
    assert "<form action={signOutEverywhere}>" in nav, (
        "the sign-out button is not a plain form posting the server action"
    )
    assert "onClick" not in "\n".join(code_lines(nav)), (
        "sign-out is wired to a click handler, so it does nothing without JavaScript"
    )
    assert '"use server"' in source("app", "actions.ts"), "actions.ts is not a server module"


def test_the_delete_form_signs_out_and_leaves_no_session_behind():
    """Delete, then sign out, then /signin — and in that order.

    Signing out first would drop the session before the delete could use it, and
    a failure after that point would leave a person who asked to be erased signed
    out, told nothing, and still in the database.
    """
    actions = body("app", "actions.ts")
    delete_at = actions.index("deleteAccount(userId, authUserId")
    signout_at = actions.index("auth.signOut()", delete_at)
    redirect_at = actions.index('redirect("/signin?deleted=1")')
    assert delete_at < signout_at < redirect_at, (
        "the delete action must delete, then sign out, then redirect to /signin"
    )
