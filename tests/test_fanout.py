"""The fan-out: one crawl, one catalog, one digest per person.

Phase 1e is the half of "crawl once, fan out" that has an inbox on the end of it.
Everything under test here is the notify path — the crawl is untouched, and the
tests that say so live in test_user_companies.py and test_user_notifications.py.

Three properties, and they fail in different directions:

    ISOLATION   each user receives only jobs from companies they watch, filtered
                by their own preferences. The failure is a job in the wrong
                inbox — visible, embarrassing, and the reason two users cannot
                share a filter row.
    INDEPENDENCE a job two people both watch is notified once PER PERSON. The
                failure is silent: the first digest takes it away from the
                second, and nothing anywhere records that it happened. This is
                the failure user_job_notifications exists to prevent.
    CONTAINMENT one user's broken send does not stop anybody else's digest, and
                writes nothing for the user it failed.

The whole file runs the production path with one substitution — the function that
talks to Resend — so what it proves about ordering and writes is true of the real
thing. Two users, two filters, two watch lists, and a fake sender per user so
"who got what" is answered by what was actually handed to the network.

The rig itself — `two_users`, `RecordingSender`, `same_filter_as_a` and the
second account's id — lives in tests/conftest.py, because tests/test_leakage.py
asks a different set of questions of the same arrangement. Two definitions of
"two users" is how one file's isolation holds while the other's leaks.
"""

from __future__ import annotations

from datetime import date

import pytest

from db.notifications import count_notified_for_user
from notify.digest import run_digest, run_fan_out
from notify.store import fetch_candidates, load_preferences, suppress_filter_backlog
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
    """Every test in this module gets the fan-out's fake, verified-domain .env.

    A one-line wrapper so the shared fixture can stay opt-in — see
    conftest.verified_sender_env for why it is not simply autouse there.
    """


# --- isolation ------------------------------------------------------------


async def test_each_user_gets_only_their_own_matches(two_users):
    """Two digests, two disjoint sets, in one run of one crawl's output."""
    await two_users.add_job("a-blr-backend", title="Backend Engineer", company_id=1)
    await two_users.add_job("b-pune-ml", title="Machine Learning Engineer",
                            company_id=2, location="Pune, India")

    sender = RecordingSender()
    fan_out = await run_fan_out(two_users, today=TODAY, sender=sender)

    assert len(sender.calls) == 2
    assert "a-blr-backend" in sender.to(TEST_EMAIL)[0]["text"]
    assert "b-pune-ml" not in sender.to(TEST_EMAIL)[0]["text"]
    assert "b-pune-ml" in sender.to(USER_B_EMAIL)[0]["text"]
    assert "a-blr-backend" not in sender.to(USER_B_EMAIL)[0]["text"]

    by_user = {email: run for _, email, run in fan_out.runs}
    assert [j["canonical_id"] for j in by_user[TEST_EMAIL].matched] == ["a-blr-backend"]
    assert [j["canonical_id"] for j in by_user[USER_B_EMAIL].matched] == ["b-pune-ml"]


async def test_a_company_you_do_not_watch_still_reaches_you(two_users):
    """THE INVERSE OF WHAT THIS ASSERTED BEFORE, AND THE SLICE IN ONE TEST.

    It used to be test_a_company_you_do_not_watch_never_reaches_you, and it was
    the watch-list clause tested where it was the sole difference: an ML role in
    Pune that B's filter wanted on every axis, kept out of their digest by nothing
    but the company it happened to be posted at.

    That is exactly what filter-first stops doing. Company tracking is unlimited
    and free, so the queue no longer asks whose board a job came from, and a job a
    person's filter wants reaches them wherever it was posted. B watches only
    Otherco and this job is at Testco; B gets it anyway.

    Asserted on fetch_candidates rather than through a digest, because the queue is
    where the clause was: the filter is applied afterwards, so a queue that still
    narrowed by company would be invisible to a test that only read an inbox.
    """
    await two_users.add_job("a-only", title="Machine Learning Engineer",
                            company_id=1, location="Pune, India")

    queued = [j["canonical_id"] for j in await fetch_candidates(two_users, USER_B_ID)]
    assert queued == ["a-only"], (
        "the queue is narrowing by company again — B does not watch Testco"
    )
    assert [j["canonical_id"] for j in await fetch_candidates(two_users, TEST_USER_ID)] == ["a-only"]


async def test_the_filter_applied_is_each_users_own(two_users):
    """Same company, same job, two filters — and only one of them wants it.

    Both users watch Testco here, so the watch list cannot be what separates
    them. A digest computed from the install's filter rather than the person's
    would send this to both.
    """
    await two_users.track_company(USER_B_ID, 1)
    await two_users.add_job("blr-backend", title="Backend Engineer", company_id=1)

    sender = RecordingSender()
    await run_fan_out(two_users, today=TODAY, sender=sender)

    assert len(sender.to(TEST_EMAIL)) == 1
    assert sender.to(USER_B_EMAIL) == [], "B's filter wants ML in Pune, not backend in Bengaluru"


# --- independence ---------------------------------------------------------


async def test_a_shared_job_is_notified_once_per_user(two_users):
    """One posting, two watchers, two emails, two receipts — and never a third.

    The failure this guards is the one with no trace: under a global flag the
    first user's digest would silence the job for the second forever. Here each
    is told once, and a second run tells neither of them again.
    """
    await two_users.track_company(USER_B_ID, 1)
    await same_filter_as_a(two_users)
    await two_users.add_job("shared", title="Backend Engineer", company_id=1)

    first = RecordingSender()
    await run_fan_out(two_users, today=TODAY, sender=first)

    assert len(first.calls) == 2
    assert all("shared" in call["text"] for call in first.calls)
    assert await two_users.notification_pairs() == {
        (TEST_USER_ID, "shared"),
        (USER_B_ID, "shared"),
    }

    second = RecordingSender()
    again = await run_fan_out(two_users, today=TODAY, sender=second)

    assert second.calls == [], "nobody hears about it twice"
    assert again.emailed == 0


async def test_one_users_send_does_not_consume_anothers_queue(two_users):
    """Ordering independence: A's digest going out changes nothing for B.

    Asserted from B's queue rather than from B's email, because the failure being
    excluded is a job leaving a queue rather than an email not arriving.
    """
    await two_users.track_company(USER_B_ID, 1)
    await two_users.add_job("shared", title="Backend Engineer", company_id=1)

    before = await fetch_candidates(two_users, USER_B_ID)
    await run_digest(two_users, user_id=TEST_USER_ID, to=TEST_EMAIL,
                     today=TODAY, sender=RecordingSender())
    after = await fetch_candidates(two_users, USER_B_ID)

    assert [j["canonical_id"] for j in before] == [j["canonical_id"] for j in after] == ["shared"]
    assert await count_notified_for_user(two_users, USER_B_ID) == 0


# --- the seed and the watch list, in the fan-out ---------------------------


async def test_a_seeded_back_catalogue_reaches_nobody(two_users):
    """The silent seed, through the whole fan-out rather than through the query.

    Both users watch this company and both filters want these jobs. Nothing is
    sent, because seeded rows are back catalogue for everybody — the property
    that used to belong to the global boolean and, since 1e, belongs to
    jobs.seeded alone.
    """
    await two_users.track_company(USER_B_ID, 1)
    await same_filter_as_a(two_users)
    for n in range(30):
        await two_users.add_job(f"seed-{n}", title="Backend Engineer",
                                company_id=1, seeded=True)

    sender = RecordingSender()
    fan_out = await run_fan_out(two_users, today=TODAY, sender=sender)

    assert sender.calls == []
    assert fan_out.emailed == 0
    assert await two_users.notification_pairs() == set()


async def test_widening_a_filter_mid_life_does_not_dump_the_catalogue(two_users):
    """The end-to-end version of the history hazard, through run_fan_out.

    THE SAME TEST WITH ITS TRIGGER CHANGED, which is the whole of what the
    filter-first slice did to this hazard. It used to be
    test_tracking_a_company_mid_life_does_not_dump_its_history: twenty ordinary
    open jobs, B starts WATCHING the company, and gets one digest containing the
    single job that arrived afterwards rather than twenty-one.

    Watching a company grants nothing now, so it cannot dump anything. Widening a
    FILTER can, and by more: a new answer admits every stored row it covers across
    the whole catalog, and none of those has a user_job_notifications row — those
    are written when something is SENT, and nothing was ever sent for a job that
    did not match. So nothing suppresses them and they would all go out as news
    because a person edited a form. That is the "entering" direction
    scripts/relabel_cities.report_blast_radius refuses to apply on, reached from
    the other side: there a labeller moved a job into a filter, here the filter
    moved onto the job.

    B's fixture filter is ML in Pune, so none of the twenty match it. Widening to
    A's filter admits all twenty at once, which is the archive dump; the delta
    suppression is what turns it into one email about the job that arrived after.
    """
    for n in range(20):
        await two_users.add_job(f"old-{n}", title="Backend Engineer", company_id=1)

    before = await load_preferences(two_users, USER_B_ID)
    await same_filter_as_a(two_users)
    after = await load_preferences(two_users, USER_B_ID)
    assert len(await suppress_filter_backlog(two_users, USER_B_ID, before, after)) == 20

    await two_users.add_job("brand-new", title="Backend Engineer", company_id=1)

    sender = RecordingSender()
    await run_fan_out(two_users, today=TODAY, sender=sender)

    assert len(sender.to(USER_B_EMAIL)) == 1
    body = sender.to(USER_B_EMAIL)[0]["text"]
    assert "brand-new" in body
    assert "old-0" not in body
    assert "1 new match" in sender.to(USER_B_EMAIL)[0]["subject"]


# --- containment ----------------------------------------------------------


async def test_one_users_failed_send_does_not_stop_the_others(two_users):
    """A refused send is that user's problem and nobody else's.

    Nothing is written for the user who failed — run_digest raises before any row
    is touched, the crawler's Guard 1 applied to email — so their jobs stay
    queued for the next run while everyone else's digest goes out normally.
    """
    await two_users.add_job("a-blr-backend", title="Backend Engineer", company_id=1)
    await two_users.add_job("b-pune-ml", title="Machine Learning Engineer",
                            company_id=2, location="Pune, India")

    sender = RecordingSender(fail_for=USER_B_EMAIL)
    fan_out = await run_fan_out(two_users, today=TODAY, sender=sender)

    assert len(sender.to(TEST_EMAIL)) == 1
    assert await count_notified_for_user(two_users, TEST_USER_ID) == 1

    failures = [email for _, email, _ in fan_out.failures]
    assert failures == [USER_B_EMAIL]
    assert await count_notified_for_user(two_users, USER_B_ID) == 0
    # MEMBERSHIP RATHER THAN EQUALITY, since the filter-first slice, and the extra
    # row is the property rather than a leak. B's queue is catalog-wide now, so it
    # also holds the job A was just emailed — A's receipt is A's alone and silences
    # nothing for B. What this test is about is that B's OWN match survived a failed
    # send, so it asserts that directly and asserts the other row on purpose.
    queued = {j["canonical_id"] for j in await fetch_candidates(two_users, USER_B_ID)}
    assert "b-pune-ml" in queued, "a failed send consumed the queue it failed on"
    assert "a-blr-backend" in queued, "A's successful send silenced a job for B"


async def test_the_sandbox_sender_still_refuses_everyone_but_my_email(two_users, monkeypatch):
    """The live install's actual state, and it must fail loudly rather than send.

    onboarding@resend.dev delivers only to the address owning the API key, so
    with it as FROM_EMAIL the second account's digest cannot be sent at all.
    assert_self_send refuses locally, before the network call and before any
    write, so the error names the real constraint instead of arriving as a 403
    that reads like an auth failure — and the fan-out contains the refusal rather
    than dying on it, so the first user's digest still goes out.

    A's own digest is deliberately in this test too. "The sandbox blocks the
    second user" and "the sandbox blocks everything" are very different states
    and only one of them is correct.
    """
    monkeypatch.setenv("FROM_EMAIL", "onboarding@resend.dev")
    await two_users.add_job("a-blr-backend", title="Backend Engineer", company_id=1)
    await two_users.add_job("b-pune-ml", title="Machine Learning Engineer",
                            company_id=2, location="Pune, India")

    sender = RecordingSender()
    fan_out = await run_fan_out(two_users, today=TODAY, sender=sender)

    assert [call["to"] for call in sender.calls] == [TEST_EMAIL]
    failures = {email: run.error for _, email, run in fan_out.failures}
    assert list(failures) == [USER_B_EMAIL]
    assert "sandbox sender" in failures[USER_B_EMAIL]
    assert await two_users.notification_pairs() == {(TEST_USER_ID, "a-blr-backend")}
    # Membership, for the reason the test above gives: B's queue is catalog-wide
    # since the filter-first slice and legitimately holds A's job too.
    assert "b-pune-ml" in {j["canonical_id"] for j in await fetch_candidates(two_users, USER_B_ID)}


async def test_a_users_digest_goes_to_their_own_address(two_users):
    """The recipient is a property of the person, not of the environment.

    Defaulting `to` to MY_EMAIL once there is more than one user would compute a
    second person's matches and post them to my inbox — a failure that sends a
    real email, passes assert_self_send, and looks like a working system.
    """
    await two_users.add_job("a-blr-backend", title="Backend Engineer", company_id=1)

    sender = RecordingSender()
    await run_fan_out(two_users, today=TODAY, sender=sender)

    assert [call["to"] for call in sender.calls] == [TEST_EMAIL]


# --- the quiet outcomes ---------------------------------------------------


async def test_a_user_with_no_matches_gets_no_email(two_users):
    """One digest, not two. A silent day for one person is not a silent run."""
    await two_users.add_job("a-blr-backend", title="Backend Engineer", company_id=1)

    sender = RecordingSender()
    fan_out = await run_fan_out(two_users, today=TODAY, sender=sender)

    assert [call["to"] for call in sender.calls] == [TEST_EMAIL]
    assert fan_out.sent == 1
    by_user = {email: run for _, email, run in fan_out.runs}
    assert by_user[USER_B_EMAIL].matched == []
    assert by_user[USER_B_EMAIL].message_id is None


async def test_a_quiet_hour_for_everyone_never_reads_the_secrets(two_users, monkeypatch):
    """Zero matches all round sends nothing and cannot fail on configuration.

    run_digest returns before email_config() for every user, so an install with a
    missing RESEND_API_KEY still exits cleanly on a quiet hour — which is exactly
    why the workflow checks the secrets up front, and why that reasoning has to
    survive the queue being rewritten. Proven by removing the key entirely.
    """
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    await two_users.add_job("nope", title="Senior Sales Director", company_id=1)

    sender = RecordingSender()
    fan_out = await run_fan_out(two_users, today=TODAY, sender=sender)

    assert sender.calls == []
    assert fan_out.sent == 0
    assert fan_out.failures == []
    assert await two_users.sent_log() == []


async def test_an_inactive_user_is_not_sent_to(two_users):
    """is_active, finally read by something.

    A deactivated account keeps its preferences, its watch list and its
    notification history and stops receiving email — which is a different and
    reversible decision from deleting the row, whose cascades take all three.
    """
    await two_users.add_job("b-pune-ml", title="Machine Learning Engineer",
                            company_id=2, location="Pune, India")
    await two_users.execute("UPDATE users SET is_active = 0 WHERE id = $1", USER_B_ID)

    sender = RecordingSender()
    fan_out = await run_fan_out(two_users, today=TODAY, sender=sender)

    assert [email for _, email, _ in fan_out.runs] == [TEST_EMAIL]
    assert sender.calls == []
    # Their queue is untouched: reactivating them must not have cost them a job.
    assert [j["canonical_id"] for j in await fetch_candidates(two_users, USER_B_ID)] == ["b-pune-ml"]


async def test_a_user_with_an_empty_watch_list_still_matches_the_catalogue(digest_conn):
    """AN EMPTY WATCH LIST IS NOT A FILTER, AND THIS IS THE SLICE STATED PLAINLY.

    It used to assert the opposite — `candidates == 0` — with a docstring saying an
    account that had picked no company "must not fall back to the whole catalog".
    That was right while the queue's first clause was the watch list: falling back
    would have meant a digest computed from a list nobody chose, which is the
    seeded-defaults failure Slices 1b and 1c removed from the preferences row.

    Under filter-first there is no fallback and no list to fall back FROM. Every
    account is matched against every board we crawl, and what narrows it to a
    person is their filter — so an account that has pinned no company is not a
    broken account or a quiet one, it is an ordinary account.

    B holds the seeded default filter, which wants a Bengaluru backend role, and
    this is one. B watches nothing. B gets it.
    """
    await digest_conn.add_user(USER_B_ID, USER_B_EMAIL)
    await digest_conn.add_preferences_for(USER_B_ID)
    await digest_conn.add_job("visible-to-a", title="Backend Engineer", company_id=1)

    sender = RecordingSender()
    fan_out = await run_fan_out(digest_conn, today=TODAY, sender=sender)

    assert fan_out.failures == []
    by_user = {email: run for _, email, run in fan_out.runs}
    assert by_user[USER_B_EMAIL].candidates == 1, (
        "an account watching nothing read an empty queue — the gate is back"
    )
    assert [j["canonical_id"] for j in by_user[USER_B_EMAIL].matched] == ["visible-to-a"]
    assert sorted(call["to"] for call in sender.calls) == sorted([TEST_EMAIL, USER_B_EMAIL])
