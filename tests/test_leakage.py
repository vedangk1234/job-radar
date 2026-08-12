"""Cross-user leakage, stated as five properties rather than as spot checks.

Phase 1e built the fan-out and 1f made the badge per-user, and both were verified
by hand: 1e by a two-user run whose output a person read, 1f by badge-check's §2
against the live database. This file is that verification turned into assertions
that run on every commit — the same move tests/test_user_companies.py made for
"the crawl has never heard of the watch list".

WHAT THIS FILE ADDS THAT test_fanout.py DOES NOT
------------------------------------------------
test_fanout.py proves each user's digest is computed and sent correctly. This one
proves the complement: that nothing of A's ever appears in B's answer, and that
every write bound to one user leaves the other's rows exactly as it found them.
Those are different questions, and the second is the one with no visible failure —
a job that leaks into the wrong digest is embarrassing, a job silently consumed
from someone else's queue is invisible until they notice they never heard about
it. So the assertions here are mostly of the shape "B's slice of the table is
byte-identical either side of A's write", not "B got the right email".

Every test that could be satisfied by a difference in FILTERS gives both users the
same filter, so the only thing that can separate them is the per-user state. A
leak hiding behind "well, B's filter did not want it anyway" would prove nothing.

THE FIVE PROPERTIES, AND WHERE EACH ONE WAS ALREADY HALF-COVERED
----------------------------------------------------------------
    1  no job crosses between two digests
       — the watch-list axis and the filter axis were each tested alone in
         test_fanout.py; this asks both at once, over all four combinations.
    2  a write for one user touches no row of another's
       — test_one_users_send_does_not_consume_anothers_queue checks B after A
         sends; this snapshots BOTH slices and runs BOTH directions.
    3  order independence
       — test_a_send_to_one_user_does_not_silence_a_job_for_another sends A then
         B; this parametrises the order, because "A then B works" is not the
         claim.
    4  a shared job is one receipt per person
       — the pair rows were asserted in 1e; the `notifications` sent-log was not,
         and a flattened send would leave one receipt where there should be two.
    5  filter-time suppression is per-user
       — every existing test looks at the person whose filter changed. None
         asserts that the person who was ALREADY matching those jobs is
         unaffected, which is the actual per-user claim. (It was tracking-time
         suppression until the filter-first slice; the trigger moved, the
         property did not.)

The dashboard half of property 3 — a job notified to A reads NOTIFIED for A and
NEW for B — cannot live here, because it is TypeScript reading a live database.
It lives in dashboard/scripts/badge-check.ts, which asserts it in both directions
against a second account inside a transaction that always rolls back.

No network anywhere: the sender is conftest.RecordingSender and descriptions are
stubbed out by the autouse fixture in conftest.py. The SQL is the production SQL.
"""

from __future__ import annotations

from datetime import date

import pytest

from notify.digest import run_fan_out
from notify.store import (
    fetch_candidates,
    load_preferences,
    record_sent,
    suppress_filter_backlog,
)
from tests.conftest import (
    TEST_EMAIL,
    TEST_USER_ID,
    USER_B_EMAIL,
    USER_B_ID,
    RecordingSender,
    same_filter_as_a,
)

TODAY = date(2026, 8, 3)


@pytest.fixture(autouse=True)
def email_env(verified_sender_env):
    """The fan-out's fake, verified-domain .env — see conftest.verified_sender_env."""


async def queued_for(conn, user_id) -> set[str]:
    """The canonical_ids currently in one user's digest queue.

    A set rather than the list fetch_candidates returns, because these assertions
    are about MEMBERSHIP and the query's ORDER BY is (company name, title) — two
    jobs with the same title at the same company have no defined order between
    them, and a test that depended on one would fail for a reason unrelated to
    anything it was written to catch.
    """
    return {job["canonical_id"] for job in await fetch_candidates(conn, user_id)}


def owned_by(pairs: set[tuple[str, str]], user_id: str) -> set[str]:
    """One user's slice of conftest.notification_pairs()."""
    return {canonical_id for owner, canonical_id in pairs if owner == user_id}


# --- 1. no job crosses between two digests --------------------------------


async def test_no_job_crosses_between_two_digests(two_users):
    """The whole matrix in one fan-out: two axes, four cells, two digests.

    THE AXES CHANGED WITH THE FILTER-FIRST SLICE AND THE SHAPE DID NOT, which is
    the thing to understand before editing this. The matrix used to be WATCH LIST
    x FILTER, and its two interesting cells were the ones satisfying one axis for
    one person and the other axis for the other — a job at A's company that only
    B's filter wanted, and the reverse. Both went to nobody, because each was
    wanted by one person and visible to the other.

    Neither of those cells exists now. There is no watch-list axis: a job at A's
    company that B's filter wants is simply B's job, which is what
    tests/test_fanout.test_a_company_you_do_not_watch_still_reaches_you asserts
    directly. Deleting the matrix outright would have been wrong, though — the
    PROPERTY it defends is that nothing of A's ever appears in B's answer, and
    that property is undiminished. It just needs the axis that still separates
    people.

    So the second axis is now NOTIFICATION STATE, which is the other per-user
    fact the queue reads:

        a-filter-untold   A's filter, nobody told    -> A only
        b-filter-untold   B's filter, nobody told    -> B only
        a-filter-told-a   A's filter, already told A -> NOBODY
        b-filter-told-b   B's filter, already told B -> NOBODY

    The two "nobody" cells are still the point, and they are sharper than the ones
    they replace: each is wanted by exactly one person's filter and has already
    been recorded against exactly that person, so the only way either reaches an
    inbox is a query that reads somebody else's notification rows.

    THE COMPANIES ARE DELIBERATELY CROSSED — A's filter's jobs sit at B's company
    and vice versa — so that a queue which quietly re-acquired a company gate
    would empty both digests rather than passing.
    """
    await two_users.add_job("a-filter-untold", title="Backend Engineer",
                            company_id=2, location="Bengaluru, India")
    await two_users.add_job("b-filter-untold", title="Machine Learning Engineer",
                            company_id=1, location="Pune, India")
    await two_users.add_job("a-filter-told-a", title="Backend Engineer",
                            company_id=2, location="Bengaluru, India")
    await two_users.add_job("b-filter-told-b", title="Machine Learning Engineer",
                            company_id=1, location="Pune, India")
    await two_users.notify_user(TEST_USER_ID, "a-filter-told-a")
    await two_users.notify_user(USER_B_ID, "b-filter-told-b")

    sender = RecordingSender()
    fan_out = await run_fan_out(two_users, today=TODAY, sender=sender)

    by_user = {email: run for _, email, run in fan_out.runs}
    assert {j["canonical_id"] for j in by_user[TEST_EMAIL].matched} == {"a-filter-untold"}
    assert {j["canonical_id"] for j in by_user[USER_B_EMAIL].matched} == {"b-filter-untold"}

    # And in the emails themselves, which is where a leak would be visible to a
    # person. Asserted in both directions rather than once: "A's job is in A's
    # email" and "A's job is NOT in B's email" are different claims, and only the
    # second one is about isolation.
    a_body = sender.to(TEST_EMAIL)[0]["text"]
    b_body = sender.to(USER_B_EMAIL)[0]["text"]
    assert "a-filter-untold" in a_body
    assert "b-filter-untold" in b_body
    for leaked in ("b-filter-untold", "a-filter-told-a", "b-filter-told-b"):
        assert leaked not in a_body
    for leaked in ("a-filter-untold", "a-filter-told-a", "b-filter-told-b"):
        assert leaked not in b_body

    # The two already-told rows kept their single owner, and the two fresh sends
    # added exactly one row each. A job that reached no inbox but acquired a
    # notification row would be silently withheld from whoever becomes entitled to
    # it later, which is the failure with no visible symptom.
    assert await two_users.notification_pairs() == {
        (TEST_USER_ID, "a-filter-untold"),
        (TEST_USER_ID, "a-filter-told-a"),
        (USER_B_ID, "b-filter-untold"),
        (USER_B_ID, "b-filter-told-b"),
    }


# --- 2. a write for one user touches no row of another's -------------------


async def test_recording_for_one_user_touches_no_row_of_the_others(two_users):
    """Both slices, snapshotted, across a send in each direction.

    The existing coverage checks B's queue after A sends. That catches the
    flattening 1d removed, and it does not catch the general form: a write bound
    to one user_id reaching a row bound to another. So this asserts the whole of
    each person's state — their queue AND their notification rows — is unmoved by
    the other's send, and then runs the send the other way round to prove the
    first direction was not simply the order the code happens to be written in.

    Both users watch the same company and hold the same filter, so nothing about
    the arrangement separates them except the rows being written.
    """
    await two_users.track_company(USER_B_ID, 1)
    await same_filter_as_a(two_users)
    await two_users.add_job("shared", title="Backend Engineer", company_id=1)
    await two_users.add_job("other", title="Backend Engineer", company_id=1)

    both = {"shared", "other"}
    assert await queued_for(two_users, TEST_USER_ID) == both
    assert await queued_for(two_users, USER_B_ID) == both

    # A is told about one of them. B must be exactly as they were.
    assert await record_sent(two_users, TEST_USER_ID, ["shared"], "2026-08-03-am") == 1

    pairs = await two_users.notification_pairs()
    assert owned_by(pairs, TEST_USER_ID) == {"shared"}
    assert owned_by(pairs, USER_B_ID) == set(), "A's send created a row for B"
    assert await queued_for(two_users, TEST_USER_ID) == {"other"}
    assert await queued_for(two_users, USER_B_ID) == both, "A's send consumed B's queue"

    # Now the other direction, over the job A was NOT told about — so a write that
    # reached across users would have to move a row that is already asymmetric,
    # which is the case a symmetric fixture cannot catch.
    assert await record_sent(two_users, USER_B_ID, ["other"], "2026-08-03-am") == 1

    pairs = await two_users.notification_pairs()
    assert owned_by(pairs, TEST_USER_ID) == {"shared"}, "B's send altered A's rows"
    assert owned_by(pairs, USER_B_ID) == {"other"}
    assert await queued_for(two_users, TEST_USER_ID) == {"other"}, "B's send consumed A's queue"
    assert await queued_for(two_users, USER_B_ID) == {"shared"}

    # The sent-log is the one table here that is NOT per-user — a receipt is a
    # record that a job went out in a digest, and two people being told about two
    # different jobs is two receipts. Asserted so that "nothing crossed" is not
    # mistaken for "nothing was written".
    assert sorted(row["canonical_id"] for row in await two_users.sent_log()) == ["other", "shared"]


# --- 3. order independence -------------------------------------------------


@pytest.mark.parametrize(
    "order",
    [(TEST_USER_ID, USER_B_ID), (USER_B_ID, TEST_USER_ID)],
    ids=["a-then-b", "b-then-a"],
)
async def test_the_order_of_two_sends_does_not_matter(two_users, order):
    """One job both users are entitled to, sent to each in turn — both ways round.

    Independence is a claim about a pair, and the existing test makes it in one
    order only. Under a global flag the first send would take the job away from
    whoever went second, so "A then B works" and "B then A works" fail in
    different halves of the same bug: testing one of them proves that half.

    The end state must be identical whichever way round it is reached, and a
    third send to either must be a genuine no-op rather than a second row.
    """
    first, second = order
    await two_users.track_company(USER_B_ID, 1)
    await same_filter_as_a(two_users)
    await two_users.add_job("shared", title="Backend Engineer", company_id=1)

    assert await record_sent(two_users, first, ["shared"], "2026-08-03-am") == 1
    assert await queued_for(two_users, second) == {"shared"}, (
        "the first send emptied the second user's queue — the flattening 1d removed"
    )

    assert await record_sent(two_users, second, ["shared"], "2026-08-03-am") == 1

    assert await two_users.notification_pairs() == {
        (TEST_USER_ID, "shared"),
        (USER_B_ID, "shared"),
    }
    assert await queued_for(two_users, TEST_USER_ID) == set()
    assert await queued_for(two_users, USER_B_ID) == set()

    for user in (first, second):
        assert await record_sent(two_users, user, ["shared"], "2026-08-04-am") == 0


# --- 4. a shared job is one receipt per person -----------------------------


async def test_a_shared_job_produces_one_receipt_per_person(two_users):
    """Two people, one posting, two receipts — through the whole fan-out.

    tests/test_fanout.py already proves both users are emailed and both get a
    notification row. What it does not look at is `notifications`, and that table
    is where a flattened send would still be visible after the fact: one receipt
    for a job two people were told about is the sent-log recording an event that
    did not happen, and it is the only durable trace of a digest there is.

    Run through run_fan_out rather than through record_sent, so the receipts are
    written by the production path with the production digest tag.
    """
    await two_users.track_company(USER_B_ID, 1)
    await same_filter_as_a(two_users)
    await two_users.add_job("shared", title="Backend Engineer", company_id=1)

    sender = RecordingSender()
    await run_fan_out(two_users, today=TODAY, tag_suffix="cron", sender=sender)

    assert sorted(call["to"] for call in sender.calls) == sorted([TEST_EMAIL, USER_B_EMAIL])
    assert await two_users.notification_pairs() == {
        (TEST_USER_ID, "shared"),
        (USER_B_ID, "shared"),
    }

    receipts = await two_users.sent_log()
    assert [row["canonical_id"] for row in receipts] == ["shared", "shared"]
    assert {row["digest"] for row in receipts} == {"2026-08-03-cron"}


# --- 5. tracking-time suppression is per-user ------------------------------


async def test_one_users_filter_save_suppresses_history_for_them_only(two_users):
    """B widens their filter onto jobs A already matches. A's queue must not move.

    THE SAME PROPERTY AS BEFORE WITH ITS TRIGGER MOVED. It was
    test_one_users_tracking_suppresses_history_for_them_only, and it asked the
    per-user half of the archive-dump guard: db.watchlist.track_companies recorded
    a company's stored jobs as already-notified for whoever had just started
    watching it, and every other test looked at that person. Nobody asserted the
    other side — that A, who had been watching all along and whose legitimate queue
    those jobs were, lost nothing.

    Filter-first moved the suppression from tracking a company to widening a
    filter, and the per-user question is identical: B's save must record twenty
    rows against B and not one against A, and A's queue must be exactly what it
    was.

    The failure this excludes is the worst-shaped one in the codebase — a job
    removed from somebody's queue by somebody else's action, leaving a row that
    says the withholding was deliberate. Nothing would ever surface it. It is also
    the failure that made the track-time suppression unsafe to keep once the gate
    was gone: pinning a company you already match would have swallowed whatever had
    arrived at it since your last save. See db/watchlist.track_companies.
    """
    for n in range(20):
        await two_users.add_job(f"old-{n}", title="Backend Engineer", company_id=1)

    history = {f"old-{n}" for n in range(20)}
    assert await queued_for(two_users, TEST_USER_ID) == history
    assert await two_users.notification_pairs() == set()

    # B's fixture filter wants ML in Pune, so none of these are theirs yet.
    before = await load_preferences(two_users, USER_B_ID)
    await same_filter_as_a(two_users)
    after = await load_preferences(two_users, USER_B_ID)
    assert set(await suppress_filter_backlog(two_users, USER_B_ID, before, after)) == history

    pairs = await two_users.notification_pairs()
    assert owned_by(pairs, USER_B_ID) == history, "B's own history was not suppressed"
    assert owned_by(pairs, TEST_USER_ID) == set(), (
        "B saving a filter recorded rows against A"
    )
    assert await queued_for(two_users, USER_B_ID) == set()
    assert await queued_for(two_users, TEST_USER_ID) == history, (
        "B saving a filter emptied A's queue"
    )
