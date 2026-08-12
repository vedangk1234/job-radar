"""Per-user notification state, its migration, and the history it must withhold.

Phase 1d moved "has this person been told about this job" out of the global
jobs.notified boolean and into user_job_notifications. The dangerous half was not
the table, it was the BACKFILL: until every historically-notified job had a row
naming the existing user, the digest's queue considered all of them fresh, and
getting that wrong meant one email containing a person's entire job history —
roughly 7,700 rows, most of them stored by the silent seed and never meant for an
inbox.

Phase 1e then made the queue genuinely per-user, and this file carries both the
tests that were written to fail when that happened and the answers they were
waiting for. Two of them said so in their own docstrings; read those before
changing anything here, because the shape of the argument matters more than the
assertions:

    test_notification_state_is_read_per_user
        was: both users see an empty queue, because the boolean flattens them
        now: one send, two independent answers

    test_the_silent_seed_is_suppressed_by_the_seeded_flag
        was: the boolean is what holds a back catalogue back
        now: jobs.seeded is, and the boolean is vestigial (asserted separately,
             which is 1g rehearsed)

The rest divide into the table behaving (uniqueness, cascades), the 1d migration
being provably correct (idempotent, exact membership, a gate that cannot pass
wrongly, and a digest that sends ZERO afterwards), and the two ways history
reaches a new inbox — a silent seed, and a user who starts tracking a company
that already has stored jobs.

The zero-send test is the whole of 1d in one assertion and is written with a
sender that raises if it is called, so "sent nothing" is proven by the absence of
a network call rather than by a count that could be zero for other reasons.

No network anywhere. The SQL is the production SQL from db/notifications.py,
db/watchlist.py and notify/store.py, run against the SQLite shim — a mock would
happily agree that a backfill covered everything without the real INSERT ever
having said so.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from db.notifications import count_notified_for_user, record_notified
from notify.digest import run_digest
from notify.store import (
    fetch_candidates,
    load_preferences,
    record_sent,
    suppress_filter_backlog,
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


async def seed_history(conn, count: int, *, matching: bool = True) -> list[str]:
    """`count` open jobs stored by a silent seed. The shape the live table is in.

    ~7,700 rows in the live table are in exactly this state: stored by a
    company's first-ever crawl and never emailed to anybody. Until Phase 1g this
    helper set the old global boolean too, because crawler/store.mark_new wrote
    both from one `seeding` argument; the boolean is gone and `seeded` is the
    whole of what a silent seed means.

    `matching` controls whether they pass the filter, because only a matching job
    can reach a digest — a re-spam test seeded with jobs the filter drops would
    pass without proving anything.
    """
    title = "Backend Engineer" if matching else "Senior Sales Director"
    ids = []
    for n in range(count):
        cid = f"hist-{n}"
        await conn.add_job(cid, title=title, seeded=True)
        ids.append(cid)
    return ids


async def unseeded_history(conn, count: int) -> list[str]:
    """`count` ordinary open jobs: not seeded, never emailed, sitting in a queue.

    The OTHER kind of history, and the one jobs.seeded says nothing about — a
    company that has been crawled normally for weeks accumulates these. They are
    what a user starting to track that company would receive in one email if
    db/watchlist.track_companies did not record them first.
    """
    ids = []
    for n in range(count):
        cid = f"old-{n}"
        await conn.add_job(cid, title="Backend Engineer")
        ids.append(cid)
    return ids


async def widen_filter(conn, user_id: str, **narrowed):
    """Give this user a filter, and return (before, after) around the change.

    THE SHAPE EVERY SUPPRESSION TEST BELOW NEEDS, and it is a helper rather than
    six copies because the interesting part is what the delta does, not how a row
    is replaced. `narrowed` is the filter they hold FIRST; they end on the seeded
    default, which wants a Bengaluru backend role and therefore matches everything
    unseeded_history() stores.

    Deleting and re-inserting rather than UPDATEing is what conftest.same_filter_as_a
    does one file over, and for the same reason: preferences_one_per_user_idx
    refuses a second row per user, so a replacement is genuinely two statements.
    """
    await conn.execute("DELETE FROM preferences WHERE user_id = $1", user_id)
    await conn.add_preferences_for(user_id, **narrowed)
    before = await load_preferences(conn, user_id)

    await conn.execute("DELETE FROM preferences WHERE user_id = $1", user_id)
    await conn.add_preferences_for(user_id)
    after = await load_preferences(conn, user_id)
    return before, after


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


async def test_record_sent_writes_the_record_and_the_receipt_and_nothing_else(digest_conn):
    """Two writes, one transaction — what replaced the 1d dual-write.

    Through 1d, 1e and 1f this function also flipped the global jobs.notified
    boolean, as a rollback net: reverting 1d could then not re-send anything that
    had gone out during the 1d window. Phase 1g removed that write and then the
    column, because by 1e nothing read it and by 1f nothing rendered from it — a
    net for a rollback nobody can still perform is not a net, it is a column the
    next person assumes something depends on.

    So what a send leaves behind is exactly two rows: the per-user record that
    stops a re-send, and the receipt that explains it.
    """
    await digest_conn.add_job("a", title="Backend Engineer")

    await record_sent(digest_conn, TEST_USER_ID, ["a"], "2026-08-03-am")

    assert await digest_conn.notification_pairs() == {(TEST_USER_ID, "a")}
    assert [row["canonical_id"] for row in await digest_conn.sent_log()] == ["a"]


async def test_notification_state_is_read_per_user(digest_conn):
    """THE 1d TRIPWIRE, NOW SHOWING THE 1e ANSWER. Read the history first.

    This test used to assert the opposite of what it asserts now, on purpose, and
    the change is the whole of Phase 1e.

    In 1d the state was already per-user but the QUEUE was not: the predicate was
    `notified = FALSE AND NOT EXISTS(my row)`, a conjunction, so a job emailed to
    me flipped the global boolean and vanished from everybody's queue. 1d was
    single-user by design and the second user had no fan-out to reach them
    anyway, so that flattening cost nothing — but it meant "per-user reads" was
    only half true, and this test ended with a line asserting BOTH users saw an
    empty queue, written specifically so that removing the boolean clause would
    fail here and make someone read the reasoning.

    That line has now been removed, deliberately, by the change it was watching
    for. The boolean clause is gone from notify/store.CANDIDATES_SQL; the queue
    asks the watch list, jobs.seeded, and this table. So the second user, who
    tracks the same company and was never emailed anything, sees the job — which
    is the entire point of the table and the failure 1d was built to prevent.

    What did change again in 1g: record_sent stopped dual-writing the boolean,
    and the column is gone. The two assertions below that used to be phrased in
    terms of it are phrased in terms of the state itself, which is what they were
    always about — a row naming a person, or the absence of one.
    """
    await digest_conn.add_user(OTHER_USER_ID, OTHER_EMAIL)
    await digest_conn.track_company(OTHER_USER_ID, 1)
    await digest_conn.add_job("a", title="Backend Engineer")

    await record_sent(digest_conn, TEST_USER_ID, ["a"], "2026-08-03-am")

    # The state knows the difference, which the boolean never could.
    assert await digest_conn.notification_pairs() == {(TEST_USER_ID, "a")}

    # And so does the queue. One send, two independent answers.
    assert [j["canonical_id"] for j in await fetch_candidates(digest_conn, TEST_USER_ID)] == []
    assert [j["canonical_id"] for j in await fetch_candidates(digest_conn, OTHER_USER_ID)] == ["a"]


async def test_a_send_to_one_user_does_not_silence_a_job_for_another(digest_conn):
    """The multi-user failure the whole table exists to prevent, stated directly.

    Two users watching the same company, one shared job. Under the 1d conjunction
    the first digest would have taken it away from the second user permanently
    and left no trace anywhere — the expensive failure, silent, with the evidence
    destroyed by the fix. Here each is told exactly once, independently, and the
    order does not matter.
    """
    await digest_conn.add_user(OTHER_USER_ID, OTHER_EMAIL)
    await digest_conn.track_company(OTHER_USER_ID, 1)
    await digest_conn.add_job("shared", title="Backend Engineer")

    assert await record_sent(digest_conn, TEST_USER_ID, ["shared"], "2026-08-03-am") == 1
    assert await record_sent(digest_conn, OTHER_USER_ID, ["shared"], "2026-08-03-am") == 1

    assert await digest_conn.notification_pairs() == {
        (TEST_USER_ID, "shared"),
        (OTHER_USER_ID, "shared"),
    }
    for user in (TEST_USER_ID, OTHER_USER_ID):
        assert [j["canonical_id"] for j in await fetch_candidates(digest_conn, user)] == []
        assert await record_sent(digest_conn, user, ["shared"], "2026-08-04-am") == 0


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


async def test_history_recorded_against_me_makes_a_digest_send_zero(digest_conn):
    """THE test. With my history recorded, my next digest emails NOTHING.

    Twenty matches the filter genuinely wants, none of them back catalogue — so
    `seeded` cannot be what holds them, and the ONLY thing keeping them out of
    this digest is a row per (me, job). That is the whole of Phase 1d stated as
    behaviour, and the sender raises if it is reached, so passing means no email
    was even attempted.

    It was called test_after_the_backfill_a_digest_sends_zero until 1g, and it
    arranged its history by running the 1d backfill over rows carrying the old
    global boolean. The backfill has been deleted along with the column it read;
    what it produced is arranged directly here instead. The property is unchanged
    and is not about a migration — it is about what a queue does with a person's
    history, forever.

    Failing this test in the real world costs one email containing every job I
    have ever been shown.
    """
    ids = await unseeded_history(digest_conn, 20)
    await record_notified(digest_conn, TEST_USER_ID, ids)

    sender = ExplodingSender()
    run = await run_digest(digest_conn, user_id=TEST_USER_ID, sender=sender)

    assert run.candidates == 0
    assert run.matched == []
    assert run.emailed == 0
    assert run.message_id is None
    assert sender.calls == 0
    assert await digest_conn.sent_log() == []


async def test_without_any_suppression_the_same_history_would_flood(digest_conn):
    """The counterfactual, so the test above is evidence rather than a tautology.

    Twenty matching open rows that nothing has marked as history — not seeded,
    not recorded against anyone. Every one is a candidate. That is what an
    unprotected queue looks like, and it is the state the two suppressions exist
    to keep the table out of.
    """
    await unseeded_history(digest_conn, 20)

    queued = await fetch_candidates(digest_conn, TEST_USER_ID)
    assert len(queued) == 20, "with nothing marking them as history, all of them queue"


async def test_the_silent_seed_is_suppressed_by_the_seeded_flag(digest_conn):
    """THE SECOND 1d TRIPWIRE, AND WHAT 1e REPLACED IT WITH.

    This test used to be called ..._by_the_boolean and it asserted that
    `notified = FALSE` in the queue predicate was what held a freshly-seeded back
    catalogue back. That was true and it was the reason the 1d queue had to be a
    conjunction — but the boolean was carrying two different facts, and the one
    about a PERSON is why a job emailed to me disappeared from everyone else's
    queue too.

    1e separated them. "This row is back catalogue" is now jobs.seeded, written
    by crawler/store.mark_new from the same `seeding` flag, read by the queue in
    the exact position the boolean occupied. The crawl path still knows nothing
    about users — a column describing a crawl is not a step towards that, and the
    invariant tests below still say so.

    So the property this file has always defended survives the rewrite: a
    company's first-ever crawl reaches NOBODY's inbox. One discovery sweep once
    seeded 6,496 rows in an afternoon, and every one of them is covered by this
    clause rather than by 6,496 rows per user in the notification table.

    If this fails, 1g dropped something it should not have.
    """
    await digest_conn.add_user(OTHER_USER_ID, OTHER_EMAIL)
    await digest_conn.track_company(OTHER_USER_ID, 1)
    await seed_history(digest_conn, 5)

    # Nothing records these against anyone, and nothing needs to.
    assert await digest_conn.notification_pairs() == set()

    for user in (TEST_USER_ID, OTHER_USER_ID):
        queued = [j["canonical_id"] for j in await fetch_candidates(digest_conn, user)]
        assert queued == [], "a silent seed is silent for every user, not just the first"


async def test_a_genuinely_new_job_still_reaches_the_queue(digest_conn):
    """The other direction: suppression must not silence anything either.

    Recording too much would pass every "nothing was re-sent" assertion in this
    file and quietly empty the digest forever, which is the failure that leaves
    no trace. So: five jobs recorded as my history, one fresh job that is not,
    and the fresh one still comes through.
    """
    ids = await unseeded_history(digest_conn, 5)
    await record_notified(digest_conn, TEST_USER_ID, ids)
    await digest_conn.add_job("brand-new", title="Backend Engineer")

    queued = [j["canonical_id"] for j in await fetch_candidates(digest_conn, TEST_USER_ID)]
    assert queued == ["brand-new"]


@pytest.mark.parametrize("relative_path", CRAWL_PATH_FILES)
@pytest.mark.parametrize(
    "forbidden",
    ("user_job_notifications", "record_notified", "suppress_company_history"),
)
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

    PHASE 1e ADDED A COLUMN TO THE CRAWL AND NOT A USER. crawler/store.mark_new
    now writes jobs.seeded beside jobs.notified, and `seeded` is deliberately
    absent from this forbidden list: it describes the CRAWL that stored the row —
    user-blind, permanent, the same answer for everybody — which is precisely why
    1e could put the silent-seed rule there instead of teaching the seed to fan
    out. The temptation this table creates is unchanged and still forbidden: one
    line in mark_new writing a per-user row would close the seed hole and make
    seeding conditional on who exists, which is the fan-out, which lives in
    notify/.
    """
    source = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
    assert forbidden not in source, f"{relative_path} must not reference {forbidden!r}"


def test_the_queue_predicate_asks_two_questions_and_not_the_watch_list():
    """The statement itself, because the change of predicate IS the slice.

    The behavioural tests above prove what the queue returns. This one proves
    what it asks, and exists because the two failure modes differ: a predicate
    that returns the right rows today by coincidence of the data is one seeded
    company away from returning the wrong ones.

    `user_companies` MUST BE ABSENT, and asserting the absence is the load-bearing
    half. The clause was the gate — "is this job's company one you asked about" —
    and while it was there an account that had pinned nothing matched nothing
    however good its keywords were. Company tracking is unlimited and free now, so
    every account effectively sees the whole catalog and a person's FILTER is what
    decides their inbox. A revert would be silent: the digest would simply go quiet
    for everyone who had not pinned anything, which reads exactly like a slow week.

    `j.notified` must be absent for the older reason. While it was there, one
    user's send silenced a job for everybody — the flattening 1d documented and 1e
    removed.
    """
    from notify.store import (
        CANDIDATES_IGNORING_NOTIFIED_SQL,
        CANDIDATES_SQL,
        SUPPRESSION_CANDIDATES_SQL,
    )

    assert "j.notified" not in CANDIDATES_SQL
    assert "user_companies" not in CANDIDATES_SQL, (
        "the queue is asking the watch list again. An account that has pinned "
        "nothing then matches nothing, and the digest goes quiet for them with "
        "nothing on any screen explaining why."
    )
    assert "j.seeded = FALSE" in CANDIDATES_SQL               # what kind
    assert "FROM user_job_notifications n" in CANDIDATES_SQL  # who already knows
    assert "j.status = 'open'" in CANDIDATES_SQL              # and the lifecycle

    # The preview query is deliberately none of the above: it answers "what would
    # my filter pick out of everything open", which is the question
    # `npm run match-parity` asks of the live database. It never carried the gate,
    # which is what made its total comparable across this slice.
    for clause in ("user_companies", "seeded", "user_job_notifications", "notified"):
        assert clause not in CANDIDATES_IGNORING_NOTIFIED_SQL

    # THE SUPPRESSION READ IS THE QUEUE MINUS ONE CLAUSE, and which one is the
    # whole of its correctness. It shares seeded and the notification anti-join —
    # so "already handled" means the same thing to both — and drops open-only, so a
    # closed job that reopens cannot arrive as news. See _ANY_STATUS.
    assert "j.seeded = FALSE" in SUPPRESSION_CANDIDATES_SQL
    assert "FROM user_job_notifications n" in SUPPRESSION_CANDIDATES_SQL
    assert "status" not in SUPPRESSION_CANDIDATES_SQL, (
        "the suppression read filters on status. A closed job that reopens keeps "
        "its canonical_id, so an open-only suppression leaves a hole one flap wide."
    )
    assert "user_companies" not in SUPPRESSION_CANDIDATES_SQL


# --- the other half of the history hazard: saving a filter -----------------
#
# It was "starting to track" until the filter-first slice, and every test below is
# the test that was here with its trigger changed. The hazard is unchanged and is
# the one jobs.seeded does not cover: a company crawled normally for weeks
# accumulates perfectly ordinary open rows — not seeded, never emailed to anybody —
# and something has to stop them all arriving in one email the moment somebody
# becomes entitled to them. What changed is what makes a person entitled. It used
# to be creating a (user, company) pair; it is now having, or widening, a filter.


async def test_saving_a_filter_does_not_deliver_the_catalogue(digest_conn):
    """THE 1e HAZARD, THIRD ORDERING: the history is already there, and the filter
    is what reaches it.

    jobs.seeded covers a company's FIRST-EVER crawl. It says nothing about the
    weeks of ordinary crawling afterwards, which leave normal open rows that were
    never emailed to anybody — and the instant somebody's filter admits them, every
    one is in their queue. Twenty jobs in one email is the cry-wolf failure by a
    door the silent seed does not watch.

    So the save records them. Note that these rows are NOT seeded and NOT notified:
    nothing about them is special, which is exactly why the suppression has to be
    attached to the act that admits them rather than to a property of a job.
    """
    await digest_conn.add_user(OTHER_USER_ID, OTHER_EMAIL)
    await unseeded_history(digest_conn, 20)

    before, after = await widen_filter(
        digest_conn, OTHER_USER_ID, cities=["pune"], keywords=["machine learning"]
    )
    assert len(await suppress_filter_backlog(digest_conn, OTHER_USER_ID, before, after)) == 20

    queued = await fetch_candidates(digest_conn, OTHER_USER_ID)
    assert queued == [], "a new filter is a subscription, not a backfill"
    assert await count_notified_for_user(digest_conn, OTHER_USER_ID) == 20


async def test_a_job_arriving_after_you_save_still_reaches_you(digest_conn):
    """The other direction, and the reason the suppression is a snapshot.

    Suppressing "everything the new filter admits" is only correct if it happens
    once, at the moment the filter changes. A job that appears afterwards is the
    whole point of having a filter, and it must come through — otherwise the
    suppression would be an off switch nobody could see.
    """
    await digest_conn.add_user(OTHER_USER_ID, OTHER_EMAIL)
    await unseeded_history(digest_conn, 5)

    before, after = await widen_filter(
        digest_conn, OTHER_USER_ID, cities=["pune"], keywords=["machine learning"]
    )
    await suppress_filter_backlog(digest_conn, OTHER_USER_ID, before, after)
    await digest_conn.add_job("posted-later", title="Backend Engineer")

    queued = [j["canonical_id"] for j in await fetch_candidates(digest_conn, OTHER_USER_ID)]
    assert queued == ["posted-later"]


async def test_saving_twice_does_not_swallow_the_jobs_that_arrived_since(digest_conn):
    """THE SHARP EDGE, and it is sharper here than it was for tracking.

    Suppression records what a filter newly admits. Run against a filter that has
    not changed it would record everything the filter matches RIGHT NOW — which
    is the person's actual queue, eaten silently, every time they touch the form.
    Tracking could only do this on a re-add of the same company; a filter is saved
    whenever anybody edits anything, so the delta is not an optimisation here, it
    is the correctness.

    NEW \\ OLD is what makes the second save a genuine no-op rather than an
    idempotent-looking one: with before == after the difference is empty by
    construction, whatever the catalogue has done in between.
    """
    await digest_conn.add_user(OTHER_USER_ID, OTHER_EMAIL)
    await unseeded_history(digest_conn, 3)

    before, after = await widen_filter(
        digest_conn, OTHER_USER_ID, cities=["pune"], keywords=["machine learning"]
    )
    await suppress_filter_backlog(digest_conn, OTHER_USER_ID, before, after)

    await digest_conn.add_job("arrived-since", title="Backend Engineer")
    assert await suppress_filter_backlog(digest_conn, OTHER_USER_ID, after, after) == []

    queued = [j["canonical_id"] for j in await fetch_candidates(digest_conn, OTHER_USER_ID)]
    assert queued == ["arrived-since"], "re-saving must not eat the queue"


async def test_narrowing_then_widening_again_does_not_re_flood(digest_conn):
    """Narrow the filter, widen it back. The history stays history.

    The suppression rows survive a narrowing — they are about a person and a job,
    not about a filter — so widening back re-records a set that is already recorded
    and inserts nothing. Anything stored in the meantime is history this person's
    filter did not want at the time, which is the same answer they got the first
    time.

    This is the untrack/re-track case, one axis over, and it is still the case that
    proves the design rather than a corner of it.
    """
    await digest_conn.add_user(OTHER_USER_ID, OTHER_EMAIL)
    await unseeded_history(digest_conn, 4)

    narrow_prefs, wide_prefs = await widen_filter(
        digest_conn, OTHER_USER_ID, cities=["pune"], keywords=["machine learning"]
    )
    await suppress_filter_backlog(digest_conn, OTHER_USER_ID, narrow_prefs, wide_prefs)

    await digest_conn.add_job("while-narrow", title="Backend Engineer")
    # Narrowing suppresses nothing — the delta is empty in that direction.
    assert await suppress_filter_backlog(digest_conn, OTHER_USER_ID, wide_prefs, narrow_prefs) == []
    # And widening back re-admits `while-narrow`, which genuinely IS history now.
    assert await suppress_filter_backlog(
        digest_conn, OTHER_USER_ID, narrow_prefs, wide_prefs
    ) == ["while-narrow"]

    assert await fetch_candidates(digest_conn, OTHER_USER_ID) == []


async def test_suppression_covers_closed_jobs_too(digest_conn):
    """EVERY job the filter admits, not only the open ones.

    A closed job is one reappearance away from being open again: UPSERT_SEEN sets
    status='open' and miss_count=0 while keeping the row's identity. Scoping the
    suppression to open rows would leave a hole exactly one flap wide, and
    `reappeared` is the set the crawler most deliberately refuses to treat as news.

    THIS IS THE ONE PROPERTY THE REPLACEMENT HAD TO RE-EARN. The old
    SUPPRESS_COMPANY_HISTORY_SQL got it for free by selecting on company_id alone;
    the filter-backlog read had to be given a status-blind variant on purpose, and
    would have shipped green without one — the failure needs a vendor to re-list a
    posting weeks later.
    """
    await digest_conn.add_user(OTHER_USER_ID, OTHER_EMAIL)
    await digest_conn.add_job("was-closed", title="Backend Engineer", status="closed")

    before, after = await widen_filter(
        digest_conn, OTHER_USER_ID, cities=["pune"], keywords=["machine learning"]
    )
    assert await suppress_filter_backlog(
        digest_conn, OTHER_USER_ID, before, after
    ) == ["was-closed"]

    await digest_conn.execute(
        "UPDATE jobs SET status = 'open', miss_count = 0 WHERE canonical_id = $1", "was-closed"
    )

    assert await fetch_candidates(digest_conn, OTHER_USER_ID) == []


async def test_suppression_covers_only_what_the_filter_admits(digest_conn):
    """The delta is the unit — not the company, and not everything open.

    The old test was test_tracking_suppresses_only_that_company, and the pair was
    the unit because tracking was what admitted jobs. The filter is the unit now,
    so the boundary is what the new answer newly matches: a job the widened filter
    still does not want must not be recorded, or the person would be silently
    barred from it if they ever widened far enough to want it.
    """
    await digest_conn.add_company(2, "Otherco")
    await digest_conn.add_user(OTHER_USER_ID, OTHER_EMAIL)
    await digest_conn.add_job("wanted", title="Backend Engineer", company_id=2)
    await digest_conn.add_job("not-wanted", title="Senior Sales Director", company_id=1)

    before, after = await widen_filter(
        digest_conn, OTHER_USER_ID, cities=["pune"], keywords=["machine learning"]
    )
    await suppress_filter_backlog(digest_conn, OTHER_USER_ID, before, after)

    # `wanted` is at a company this account has never pinned, which is the point:
    # the filter reaches across the catalog, so the suppression has to as well.
    assert await digest_conn.notification_pairs() == {(OTHER_USER_ID, "wanted")}


async def test_a_silent_day_sends_nothing_and_writes_nothing(digest_conn):
    """Zero matches is a real outcome, not an error — unchanged by 1d.

    run_digest returns before email_config() is ever reached, so a quiet day
    cannot fail on a missing secret and cannot write a row. This is the property
    the workflow's preflight secret check exists to compensate for, and it has to
    survive the queue being rewritten.
    """
    await digest_conn.add_job("nope", title="Senior Sales Director")

    sender = ExplodingSender()
    run = await run_digest(digest_conn, user_id=TEST_USER_ID, sender=sender)

    assert run.matched == []
    assert run.emailed == 0
    assert sender.calls == 0
    assert await digest_conn.notification_pairs() == set()
    assert await digest_conn.sent_log() == []
