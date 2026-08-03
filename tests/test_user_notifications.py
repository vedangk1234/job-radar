"""Per-user notification state, and the migration that must not re-spam an inbox.

Phase 1d moves "has this person been told about this job" out of the global
jobs.notified boolean and into user_job_notifications. The dangerous half is not
the table, it is the BACKFILL: until every historically-notified job has a row
naming the existing user, the digest's queue considers all of them fresh. Getting
that wrong means one email containing a person's entire job history — roughly
7,700 rows, most of them stored by the silent seed and never meant for an inbox.

So the tests below are weighted accordingly. Four of them are about the table
behaving (uniqueness, per-user reads, cascade), and four are about the migration
being provably correct before it is trusted:

    the backfill is idempotent                    (re-runnable after a failure)
    the backfill covers exactly the right set      (membership, not arithmetic)
    the gate refuses when something is uncovered   (it cannot pass wrongly)
    a digest after the backfill sends ZERO         (the behaviour that matters)

The last one is the whole sub-phase in a single assertion, and it is written with
a sender that raises if it is called, so "sent nothing" is proven by the absence
of a network call rather than by a count that could be zero for other reasons.

No network anywhere. The SQL is the production SQL from db/notifications.py and
notify/store.py, run against the SQLite shim — a mock would happily agree that a
backfill covered everything without the real INSERT ever having said so.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from db.notifications import (
    backfill_from_notified,
    count_jobs_flagged_notified,
    count_notified_for_user,
    count_uncovered,
    record_notified,
)
from notify.digest import run_digest
from notify.store import (
    CANDIDATES_IGNORING_NOTIFIED_SQL,
    fetch_candidates,
    record_sent,
)
from tests.conftest import TEST_USER_ID

OTHER_USER_ID = "00000000-0000-4000-8000-000000000002"
OTHER_EMAIL = "someone-else@example.test"

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Imported by name rather than by reference so this list and the watch list's
# stay independently editable — the two tables are forbidden on the crawl path
# for related but not identical reasons, and a shared constant would make
# loosening one silently loosen the other.
CRAWL_PATH_FILES = [
    "scripts/crawl.py",
    "crawler/loop.py",
    "crawler/store.py",
    "crawler/diff.py",
    "crawler/enrich.py",
    "fetchers/http.py",
    "fetchers/normalize.py",
    "fetchers/tier1.py",
    "fetchers/tier3.py",
    "fetchers/workday.py",
]


class ExplodingSender:
    """A sender that fails the test if a digest ever tries to send through it.

    The point of the parity test is that NOTHING goes out. A FakeResend that
    counted calls would let that be asserted after the fact; this makes the
    failure land on the line that tried, with a message naming what happened.
    """

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, subject, html_body, text_body, **kwargs):
        self.calls += 1
        raise AssertionError(
            f"a digest tried to SEND after the backfill: {subject!r}. "
            f"Every historical match should already be recorded as notified."
        )


async def queued_by_per_user_clause(conn, user_id) -> list[str]:
    """The queue as 1e will read it: the per-user clause WITHOUT the boolean.

    Built by adding the NOT EXISTS to the ignore-notified statement rather than
    by writing a fresh query, so what is exercised is production's own SELECT
    list and join with exactly one predicate swapped. Two tests need it: one to
    show the state is genuinely per-user underneath the conjunction, and one to
    show what happens when the backfill has not run.
    """
    per_user_only = CANDIDATES_IGNORING_NOTIFIED_SQL.replace(
        "WHERE j.status = 'open'",
        "WHERE j.status = 'open'\n"
        "   AND NOT EXISTS (SELECT 1 FROM user_job_notifications n\n"
        "                    WHERE n.user_id = $1 AND n.canonical_id = j.canonical_id)",
    )
    return [row["canonical_id"] for row in await conn.fetch(per_user_only, user_id)]


async def seed_history(conn, count: int, *, matching: bool = True) -> list[str]:
    """`count` open jobs already flagged notified = TRUE. The pre-1d table.

    This is the shape the live database is actually in: ~7,700 rows carrying the
    boolean, almost all of them from the silent seed rather than from a real
    email. `matching` controls whether they pass the filter, because only a
    matching job can reach a digest — a re-spam test seeded with jobs the filter
    drops would pass without proving anything.
    """
    title = "Backend Engineer" if matching else "Senior Sales Director"
    ids = []
    for n in range(count):
        cid = f"hist-{n}"
        await conn.add_job(cid, title=title, notified=True)
        ids.append(cid)
    return ids


# --- the table behaving ---------------------------------------------------


async def test_a_second_notify_of_the_same_pair_is_a_no_op(digest_conn):
    """Double-notify inserts nothing. The UNIQUE does the work, not the caller.

    Run through the production statement rather than through conftest's direct
    helper, because what has to be safe to repeat is the thing production
    repeats.
    """
    await digest_conn.add_job("a", title="Backend Engineer")

    first = await record_notified(digest_conn, TEST_USER_ID, ["a"])
    second = await record_notified(digest_conn, TEST_USER_ID, ["a"])

    assert first == 1
    assert second == 0, "the second insert must report nothing genuinely written"
    assert await digest_conn.notification_pairs() == {(TEST_USER_ID, "a")}


async def test_record_sent_twice_does_not_duplicate_or_re_queue(digest_conn):
    """The whole write path, twice. One row, and the job stays out of the queue."""
    await digest_conn.add_job("a", title="Backend Engineer")

    assert await record_sent(digest_conn, TEST_USER_ID, ["a"], "2026-08-03-am") == 1
    assert await record_sent(digest_conn, TEST_USER_ID, ["a"], "2026-08-03-pm") == 0

    assert await digest_conn.notification_pairs() == {(TEST_USER_ID, "a")}
    assert [j["canonical_id"] for j in await fetch_candidates(digest_conn, TEST_USER_ID)] == []


async def test_record_sent_also_writes_the_boolean_rollback_net(digest_conn):
    """1d dual-writes on purpose: the new table decides, the boolean is the net.

    If this ever fails, reverting 1d would re-send everything that went out
    during the 1d window, and scripts/backfill_user_notifications.py would have
    an inaccurate source to re-run against. The column is not dead weight until
    1g drops it — see db/schema.sql.
    """
    await digest_conn.add_job("a", title="Backend Engineer")

    await record_sent(digest_conn, TEST_USER_ID, ["a"], "2026-08-03-am")

    assert (await digest_conn.snapshot())["a"]["notified"] == 1
    assert await digest_conn.notification_pairs() == {(TEST_USER_ID, "a")}
    assert [row["canonical_id"] for row in await digest_conn.sent_log()] == ["a"]


async def test_notification_state_is_recorded_per_user(digest_conn):
    """The state itself is per-user. What READS it is still globally gated in 1d.

    This is the one place the dual-write's cost is visible, and it is worth being
    precise about rather than discovering in 1e.

    record_sent writes two things: the per-user row (the authority) and the old
    global boolean (the rollback net). The 1d queue is the conjunction of both,
    so a job I have been emailed about is suppressed for EVERYBODY until the
    boolean clause comes out. That is not a bug in 1d — 1d is single-user by
    design, and the second user has no watch-list fan-out to reach them anyway
    (that is 1e). But it does mean "per-user reads" is only half true today, and
    the half that IS true is the half this table exists to make possible.

    So: the recorded STATE distinguishes the two users correctly, and the 1e
    predicate over that state gives the right answer for each. Only the retained
    boolean flattens them, and removing it is exactly what 1e/1g do.
    """
    await digest_conn.add_user(OTHER_USER_ID, OTHER_EMAIL)
    await digest_conn.add_job("a", title="Backend Engineer")

    await record_sent(digest_conn, TEST_USER_ID, ["a"], "2026-08-03-am")

    # The state knows the difference, which the boolean never could.
    assert await digest_conn.notification_pairs() == {(TEST_USER_ID, "a")}
    assert await count_uncovered(digest_conn, TEST_USER_ID) == 0
    assert await count_uncovered(digest_conn, OTHER_USER_ID) == 1

    # And the per-user predicate alone — what 1e will read — answers per user.
    assert await queued_by_per_user_clause(digest_conn, TEST_USER_ID) == []
    assert await queued_by_per_user_clause(digest_conn, OTHER_USER_ID) == ["a"]

    # Today's ACTUAL queue is still the conjunction, so both are empty. Asserted
    # rather than left implicit, so that when 1e removes the boolean clause this
    # line fails and forces someone to read the paragraph above.
    assert [j["canonical_id"] for j in await fetch_candidates(digest_conn, TEST_USER_ID)] == []
    assert [j["canonical_id"] for j in await fetch_candidates(digest_conn, OTHER_USER_ID)] == []


async def test_deleting_a_user_takes_their_notification_rows(digest_conn):
    """ON DELETE CASCADE on the user side.

    enforce_foreign_keys() is not optional here: SQLite ignores every REFERENCES
    clause unless the pragma is on, so without it this test passes vacuously with
    the rows sitting there untouched.
    """
    digest_conn.enforce_foreign_keys()
    await digest_conn.add_user(OTHER_USER_ID, OTHER_EMAIL)
    await digest_conn.add_job("a", title="Backend Engineer")
    await digest_conn.notify_user(TEST_USER_ID, "a")
    await digest_conn.notify_user(OTHER_USER_ID, "a")

    await digest_conn.execute("DELETE FROM users WHERE id = $1", OTHER_USER_ID)

    assert await digest_conn.notification_pairs() == {(TEST_USER_ID, "a")}


async def test_deleting_a_job_takes_every_users_notification_row(digest_conn):
    """ON DELETE CASCADE on the job side — the anti-join must never see an orphan."""
    digest_conn.enforce_foreign_keys()
    await digest_conn.add_user(OTHER_USER_ID, OTHER_EMAIL)
    await digest_conn.add_job("a", title="Backend Engineer")
    await digest_conn.add_job("b", title="Backend Engineer")
    await digest_conn.notify_user(TEST_USER_ID, "a")
    await digest_conn.notify_user(OTHER_USER_ID, "a")
    await digest_conn.notify_user(TEST_USER_ID, "b")

    await digest_conn.execute("DELETE FROM jobs WHERE canonical_id = $1", "a")

    assert await digest_conn.notification_pairs() == {(TEST_USER_ID, "b")}


# --- the migration -------------------------------------------------------


async def test_the_backfill_covers_exactly_the_notified_jobs(digest_conn):
    """Membership, not arithmetic: the right jobs, and only those.

    The unnotified job is the one that matters. A backfill that recorded every
    job rather than every NOTIFIED job would silently withhold the queue — the
    failure with no trace, and the opposite of the one everybody watches for.
    """
    await seed_history(digest_conn, 3)
    await digest_conn.add_job("fresh", title="Backend Engineer", notified=False)

    recorded = await backfill_from_notified(digest_conn, TEST_USER_ID)

    assert sorted(recorded) == ["hist-0", "hist-1", "hist-2"]
    assert await digest_conn.notification_pairs() == {
        (TEST_USER_ID, "hist-0"),
        (TEST_USER_ID, "hist-1"),
        (TEST_USER_ID, "hist-2"),
    }
    assert await count_uncovered(digest_conn, TEST_USER_ID) == 0


async def test_the_backfill_is_idempotent(digest_conn):
    """Run it twice. The second run writes nothing and breaks nothing.

    This is what makes a half-failed migration recoverable by re-running rather
    than by reasoning about how far it got — which matters because the gate is
    deliberately strict about mid-run arrivals and re-running is the remedy.
    """
    await seed_history(digest_conn, 3)

    first = await backfill_from_notified(digest_conn, TEST_USER_ID)
    before = await digest_conn.notification_pairs()
    second = await backfill_from_notified(digest_conn, TEST_USER_ID)

    assert len(first) == 3
    assert second == [], "a second run must insert nothing at all"
    assert await digest_conn.notification_pairs() == before
    assert await count_notified_for_user(digest_conn, TEST_USER_ID) == 3


async def test_the_gate_refuses_when_a_notified_job_is_uncovered(digest_conn):
    """Delete one row and the gate must fail. Every uncovered job is a re-send.

    The count equality alone would also catch this one — the point of asserting
    both here is the NEXT test, where it would not.
    """
    await seed_history(digest_conn, 3)
    await backfill_from_notified(digest_conn, TEST_USER_ID)

    await digest_conn.execute(
        "DELETE FROM user_job_notifications WHERE canonical_id = $1", "hist-1"
    )

    assert await count_uncovered(digest_conn, TEST_USER_ID) == 1
    assert await count_notified_for_user(digest_conn, TEST_USER_ID) == 2
    assert await count_jobs_flagged_notified(digest_conn) == 3


async def test_the_gate_is_not_fooled_by_counts_that_happen_to_match(digest_conn):
    """Why the gate is an anti-join and not just arithmetic.

    Right count, wrong membership: one historical job is uncovered and one row
    names a job that was never notified. `mine == flagged` passes — and the
    uncovered job is still in the queue, waiting to be re-sent. This is the exact
    shape of a migration that looks green and re-spams anyway.
    """
    await seed_history(digest_conn, 3)
    await digest_conn.add_job("fresh", title="Backend Engineer", notified=False)

    await digest_conn.notify_user(TEST_USER_ID, "hist-0")
    await digest_conn.notify_user(TEST_USER_ID, "hist-2")
    await digest_conn.notify_user(TEST_USER_ID, "fresh")

    assert await count_notified_for_user(digest_conn, TEST_USER_ID) == 3
    assert await count_jobs_flagged_notified(digest_conn) == 3
    assert await count_uncovered(digest_conn, TEST_USER_ID) == 1, (
        "the counts match and hist-1 is still uncovered — arithmetic alone "
        "would have passed this migration"
    )


async def test_the_gate_ignores_rows_belonging_to_another_user(digest_conn):
    """Scoped to one user, so a second account cannot make my migration look done."""
    await digest_conn.add_user(OTHER_USER_ID, OTHER_EMAIL)
    await seed_history(digest_conn, 2)

    await digest_conn.notify_user(OTHER_USER_ID, "hist-0")
    await digest_conn.notify_user(OTHER_USER_ID, "hist-1")

    assert await count_notified_for_user(digest_conn, TEST_USER_ID) == 0
    assert await count_uncovered(digest_conn, TEST_USER_ID) == 2


# --- the behaviour that is the whole point --------------------------------


async def test_after_the_backfill_a_digest_sends_zero(digest_conn):
    """THE test. With the backfill applied, my next digest emails NOTHING.

    Twenty historical matches, all flagged notified = TRUE exactly as the live
    table has them, all of them jobs the filter genuinely wants. Before 1d they
    were suppressed by the boolean; after the backfill they are suppressed by a
    row naming me. The sender raises if it is reached, so passing means no email
    was even attempted.

    Failing this test in the real world costs one email containing every job I
    have ever been shown.
    """
    await seed_history(digest_conn, 20)
    await backfill_from_notified(digest_conn, TEST_USER_ID)

    sender = ExplodingSender()
    run = await run_digest(digest_conn, user_id=TEST_USER_ID, sender=sender)

    assert run.candidates == 0
    assert run.matched == []
    assert run.emailed == 0
    assert run.message_id is None
    assert sender.calls == 0
    assert await digest_conn.sent_log() == []


async def test_without_the_backfill_the_same_digest_would_flood(digest_conn):
    """The counterfactual, so the test above is evidence rather than a tautology.

    Same twenty rows, backfill NOT run, and the per-user table read alone — which
    is what 1d would do if the queue predicate dropped the boolean clause. Every
    single historical match comes back as a candidate.

    This is the failure being prevented, written down and executed once, so that
    "the backfill matters" is a thing the suite demonstrates rather than a thing
    the docstrings claim.
    """
    await seed_history(digest_conn, 20)

    assert len(await queued_by_per_user_clause(digest_conn, TEST_USER_ID)) == 20, (
        "unbackfilled, every historical match is queued again"
    )


async def test_the_silent_seed_stays_suppressed_by_the_boolean(digest_conn):
    """The seed hole, and why the 1d queue is a conjunction rather than a swap.

    crawler/store.mark_new stamps notified = TRUE across a whole back catalogue
    on a company's first-ever crawl, and the crawl path is user-blind by design —
    so those rows have NO per-user record and never will until 1e teaches the
    seed to fan out. A queue reading only the new table would hand every one of
    them to the next digest; one discovery sweep once seeded 6,496 rows.

    Until 1e, the boolean is what holds them back. If this test fails, 1g dropped
    the column too early.
    """
    await seed_history(digest_conn, 5)

    # Deliberately NOT backfilled: this is a company seeded after the migration.
    assert await digest_conn.notification_pairs() == set()

    queued = [j["canonical_id"] for j in await fetch_candidates(digest_conn, TEST_USER_ID)]
    assert queued == [], "seeded rows have no per-user record; the boolean must still hold them"


async def test_a_genuinely_new_job_still_reaches_the_queue(digest_conn):
    """The other direction: 1d must not silence anything either.

    A backfill that over-recorded would pass every gate above and quietly empty
    the digest forever, which is the failure that leaves no trace. So: history
    backfilled, one fresh job, and the fresh one still comes through.
    """
    await seed_history(digest_conn, 5)
    await backfill_from_notified(digest_conn, TEST_USER_ID)
    await digest_conn.add_job("brand-new", title="Backend Engineer", notified=False)

    queued = [j["canonical_id"] for j in await fetch_candidates(digest_conn, TEST_USER_ID)]
    assert queued == ["brand-new"]


@pytest.mark.parametrize("relative_path", CRAWL_PATH_FILES)
@pytest.mark.parametrize("forbidden", ("user_job_notifications", "record_notified"))
def test_no_crawl_path_module_mentions_per_user_notification_state(relative_path, forbidden):
    """The crawl stays user-blind, and this table must not be the thing that ends it.

    The same invariant tests/test_user_companies.py states for the watch list,
    for the same reason and with sharper teeth here. The crawl writes
    jobs.notified — that is the silent seed — and the temptation this table
    creates is obvious: teach mark_new to write a per-user row too and the seed
    hole closes in one line. It also makes seeding conditional on who exists,
    which is the fan-out, which is 1e.

    Note that crawler/enrich.py imports notify/store.py, which DOES name this
    table. That is fine and is not a loophole: notify/store.py has been a shared
    module since enrichment moved into the crawl, it has carried a user-scoped
    statement since 1b (PREFERENCES_SQL), and the two functions enrichment calls
    from it touch no user state at all. The invariant is about what a crawl-path
    module itself says, because that is what an edit can change locally.
    """
    source = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
    assert forbidden not in source, f"{relative_path} must not reference {forbidden!r}"


async def test_a_silent_day_sends_nothing_and_writes_nothing(digest_conn):
    """Zero matches is a real outcome, not an error — unchanged by 1d.

    run_digest returns before email_config() is ever reached, so a quiet day
    cannot fail on a missing secret and cannot write a row. This is the property
    the workflow's preflight secret check exists to compensate for, and it has to
    survive the queue being rewritten.
    """
    await digest_conn.add_job("nope", title="Senior Sales Director", notified=False)

    sender = ExplodingSender()
    run = await run_digest(digest_conn, user_id=TEST_USER_ID, sender=sender)

    assert run.matched == []
    assert run.emailed == 0
    assert sender.calls == 0
    assert await digest_conn.notification_pairs() == set()
    assert await digest_conn.sent_log() == []
