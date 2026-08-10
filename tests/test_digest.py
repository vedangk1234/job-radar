"""The digest run end to end, against the real SQL and a fake Resend.

Everything here runs the production code path — the actual SELECT from
notify/store.py, the actual is_match, the actual record_sent — with exactly one
thing replaced: the function that talks to the network. That substitution is the
whole point. Nothing else about the run differs, so what these tests prove about
ordering and writes is true of the real thing.

Two properties matter more than the rest:

  * a job is emailed exactly once per person, ever. Rows already recorded
    against that person never re-enter
    the queue, and a successful send flips precisely the jobs it included.
  * a failed send writes nothing. Not a flag, not a receipt. The queue is
    identical afterwards, so the next run retries the same set — the same
    "when in doubt, change nothing" rule the crawler's guards follow.
"""

from __future__ import annotations

from datetime import date

import pytest

from notify.digest import (
    DigestError,
    assert_self_send,
    group_by_company,
    render_html,
    render_text,
    run_digest,
    subject_line,
)
from db.notifications import record_notified
from db.users import resolve_current_user_id
from notify.match import Preferences
from notify.store import fetch_candidates, load_preferences, record_sent
from tests.conftest import TEST_EMAIL, TEST_USER_ID

TODAY = date(2026, 7, 26)


class FakeResend:
    """Stands in for send_via_resend. Records the call, or fails on demand."""

    def __init__(self, message_id: str = "msg_abc123", fail: Exception | None = None) -> None:
        self.message_id = message_id
        self.fail = fail
        self.calls: list[dict] = []

    async def __call__(self, subject, html_body, text_body, *, api_key, from_email, to_email):
        self.calls.append(
            {
                "subject": subject,
                "html": html_body,
                "text": text_body,
                "from": from_email,
                "to": to_email,
            }
        )
        if self.fail:
            raise self.fail
        return self.message_id


@pytest.fixture(autouse=True)
def email_env(monkeypatch):
    """A complete, obviously-fake .env so email_config() never reads the real one."""
    monkeypatch.setenv("RESEND_API_KEY", "re_test_key")
    monkeypatch.setenv("FROM_EMAIL", "onboarding@resend.dev")
    monkeypatch.setenv("MY_EMAIL", "me@example.test")


async def queue_ids(conn) -> set[str]:
    """What this user's next digest would consider — the real queue.

    Until Phase 1g this was `SELECT ... WHERE notified = 0`, which was the queue
    while the queue was one global boolean. It is now three clauses across three
    tables, so asking the production query is the only way to ask the real
    question; a restatement here would be a second definition of the queue that
    could agree with the first for a while.
    """
    return {job["canonical_id"] for job in await fetch_candidates(conn, TEST_USER_ID)}


# The columns that describe where a job is in its LIFECYCLE, as opposed to what
# the job is. These are the ones a failed send must leave exactly as it found
# them; see test_a_failed_send_changes_absolutely_nothing.
LIFECYCLE_COLUMNS = ("seeded", "status", "first_seen_at", "last_seen_at", "miss_count")


async def lifecycle(conn) -> dict[str, dict]:
    """{canonical_id: the lifecycle columns only}, for before/after comparison."""
    return {
        cid: {col: row[col] for col in LIFECYCLE_COLUMNS}
        for cid, row in (await conn.snapshot()).items()
    }


# --- selection ------------------------------------------------------------


async def test_only_unsent_open_and_matching_jobs_are_selected(digest_conn):
    """Three independent filters, and all three have to agree.

    The already-sent row is the one that matters: it matches the filter perfectly
    and must still never appear, because this user has been told about it. Since
    Phase 1e "told" means a user_job_notifications row rather than the global
    boolean — see add_sent_job, which writes the state production writes.
    """
    await digest_conn.add_job("wanted", title="Backend Engineer")
    await digest_conn.add_job("also-wanted", title="Platform Engineer", location="Pune, India")
    await digest_conn.add_sent_job("already-sent", title="Backend Engineer")
    await digest_conn.add_job("closed", title="Backend Engineer", status="closed")
    await digest_conn.add_job("wrong-city", title="Backend Engineer", location="Berlin, Germany")
    await digest_conn.add_job("excluded", title="Senior Backend Engineer")
    await digest_conn.add_job("no-keyword", title="Customer Success Associate")

    resend = FakeResend()
    run = await run_digest(digest_conn, user_id=TEST_USER_ID, today=TODAY, sender=resend)

    # closed and already-sent never even reach the matcher
    assert run.candidates == 5
    assert {j["canonical_id"] for j in run.matched} == {"wanted", "also-wanted"}
    assert len(resend.calls) == 1


async def test_a_matching_but_already_sent_job_is_never_re_sent(digest_conn):
    """The exactly-once guarantee, stated directly — per user, since 1d/1e."""
    await digest_conn.add_sent_job("sent-yesterday", title="Backend Engineer")

    run = await run_digest(digest_conn, user_id=TEST_USER_ID, today=TODAY, sender=FakeResend())

    assert run.candidates == 0
    assert run.matched == []
    assert run.emailed == 0


async def test_zero_matches_sends_nothing_at_all(digest_conn):
    """No empty digests. An email that says "nothing today" trains you to ignore it."""
    await digest_conn.add_job("nope", title="Senior Sales Manager")

    resend = FakeResend()
    run = await run_digest(digest_conn, user_id=TEST_USER_ID, today=TODAY, sender=resend)

    assert run.matched == []
    assert resend.calls == []      # the network was never touched
    assert run.subject is None
    assert not run.sent
    assert await digest_conn.sent_log() == []
    assert await queue_ids(digest_conn) == {"nope"}  # still queued, still unsent


async def test_candidates_query_respects_the_queue_but_preview_can_ignore_it(digest_conn):
    """--ignore-notified widens the read and only the read. It writes nothing.

    Widened means the WHOLE queue filter goes: the watch list, jobs.seeded and
    the per-user rows alike. That is what keeps its total comparable with
    `npm run match-parity`, which walks every open row in the live database.
    """
    await digest_conn.add_job("queued", title="Backend Engineer")
    await digest_conn.add_sent_job("sent", title="Backend Engineer")
    await digest_conn.add_sent_job("closed", title="Backend Engineer", status="closed")

    assert {j["canonical_id"] for j in await fetch_candidates(digest_conn, TEST_USER_ID)} == {"queued"}
    everything = await fetch_candidates(digest_conn, TEST_USER_ID, ignore_notified=True)
    assert {j["canonical_id"] for j in everything} == {"queued", "sent"}  # still open-only


async def test_preferences_are_read_from_the_database_not_hardcoded(digest_conn):
    """Edit the row, and the very next run obeys it. That is the Step 5 contract."""
    await digest_conn.add_job("berlin", title="Backend Engineer", location="Berlin, Germany")
    assert await run_digest(digest_conn, user_id=TEST_USER_ID, today=TODAY, sender=FakeResend()) is not None
    assert await queue_ids(digest_conn) == {"berlin"}  # dropped on city

    # `non-india` was spelled `other` until the 25-city rewrite, which split
    # that label into "an Indian city we did not list" and "not India". Berlin
    # is the second one.
    await digest_conn.set_preferences(
        cities=["blr", "non-india"], keywords=["backend"], exclusions=["senior"]
    )
    run = await run_digest(digest_conn, user_id=TEST_USER_ID, today=TODAY, sender=FakeResend())

    assert [j["canonical_id"] for j in run.matched] == ["berlin"]
    assert await queue_ids(digest_conn) == set()


async def test_load_preferences_falls_back_when_the_row_is_missing(digest_conn):
    await digest_conn.execute("DELETE FROM preferences")
    assert await load_preferences(digest_conn, TEST_USER_ID) == Preferences()


# --- whose filter is it (Phase 1b: preferences belong to a user) -----------


async def test_preferences_are_selected_by_owner_not_by_a_fixed_id(digest_conn):
    """The row is found by user_id. A different user does not see it.

    The point is that the query is keyed on the owner rather than on the old
    `WHERE id = 1`: ask as somebody else and the seeded row — which is still
    sitting there at id 1 — is simply not yours, so the defaults answer.
    """
    await digest_conn.set_preferences(cities=["blr"], keywords=["backend"])

    mine = await load_preferences(digest_conn, TEST_USER_ID)
    assert mine.cities == ("blr",)
    assert mine.keywords == ("backend",)

    stranger = await load_preferences(digest_conn, "00000000-0000-4000-8000-0000000000ff")
    assert stranger == Preferences()


async def test_an_unowned_row_raises_instead_of_falling_back_to_defaults(digest_conn):
    """The nullable window between applying the schema and running the backfill.

    This is the one silence that must not be treated as "you have no
    preferences". The row exists and holds a filter someone tuned; it just has
    no owner yet. Falling back to the seeded defaults here would quietly email
    the results of a filter nobody chose, which is precisely the class of
    failure the crawl guards exist to prevent — so it raises, and the message
    names the script that fixes it.
    """
    await digest_conn.set_preferences(cities=["blr"], keywords=["backend"], user_id=None)

    with pytest.raises(RuntimeError, match="backfill_preferences_user"):
        await load_preferences(digest_conn, TEST_USER_ID)


# The four tests below cover db.users.resolve_current_user_id, which since Phase
# 2b is NOT part of any digest run — run_digest is handed a user and the fan-out
# reads its ids out of public.users. It survives for the operator scripts that
# have no session (scripts/preview_digest.py and friends), so what these assert is
# that the scripts' lookup is still strict: case-insensitive like the index,
# loud about an unset variable, the placeholder, and an address nothing was
# seeded for. tests/test_identity_bridge.py is what states where it may be called.


async def test_the_current_user_bridge_resolves_my_email(digest_conn):
    assert await resolve_current_user_id(digest_conn) == TEST_USER_ID


async def test_the_current_user_bridge_is_case_insensitive(digest_conn, monkeypatch):
    """Matches users_email_lower_idx: identity does not depend on shift keys."""
    monkeypatch.setenv("MY_EMAIL", TEST_EMAIL.upper())
    assert await resolve_current_user_id(digest_conn) == TEST_USER_ID


async def test_the_current_user_bridge_refuses_an_unset_my_email(digest_conn, monkeypatch):
    monkeypatch.setenv("MY_EMAIL", "")
    with pytest.raises(RuntimeError, match="MY_EMAIL is not set"):
        await resolve_current_user_id(digest_conn)


async def test_the_current_user_bridge_refuses_the_placeholder(digest_conn, monkeypatch):
    monkeypatch.setenv("MY_EMAIL", "you@example.com")
    with pytest.raises(RuntimeError, match="placeholder"):
        await resolve_current_user_id(digest_conn)


async def test_the_current_user_bridge_refuses_an_unseeded_address(digest_conn, monkeypatch):
    """A valid address with no users row names the script that creates one."""
    monkeypatch.setenv("MY_EMAIL", "someone-else@example.test")
    with pytest.raises(RuntimeError, match="seed_user"):
        await resolve_current_user_id(digest_conn)


async def test_run_digest_refuses_to_guess_whose_digest_it_is(digest_conn):
    """Phase 2b: user_id is required, and the inversion is the point.

    This test used to assert the opposite — that omitting user_id resolved the
    MY_EMAIL user — and that was safe for exactly as long as the one caller who
    omitted it and the one account that existed named the same person. A default
    is a guess about whose filter, whose queue and whose inbox this is, and the
    failure mode of guessing wrong is not an error: it is a correct digest,
    computed properly, delivered to somebody it is not about, and recorded as
    delivered against the wrong user. So the parameter has no default, and a
    caller that has not decided cannot proceed.
    """
    await digest_conn.add_job("blr-backend", title="Backend Engineer")

    with pytest.raises(TypeError, match="user_id"):
        await run_digest(digest_conn, today=TODAY, sender=FakeResend())

    assert await digest_conn.sent_log() == []


async def test_run_digest_applies_the_filter_of_the_user_it_was_given(digest_conn):
    """The other half: the id it is handed is the id it acts on.

    The seeded row belongs to TEST_USER_ID, so a run for that user applies it —
    and this is what makes the fan-out's per-user loop mean anything. See
    tests/test_leakage.py for the same claim asserted between two accounts.
    """
    await digest_conn.set_preferences(cities=["blr"], keywords=["backend"])
    await digest_conn.add_job("blr-backend", title="Backend Engineer")

    run = await run_digest(digest_conn, user_id=TEST_USER_ID, today=TODAY, sender=FakeResend())

    assert run.user_id == TEST_USER_ID
    assert [j["canonical_id"] for j in run.matched] == ["blr-backend"]


# --- flag update on success ----------------------------------------------


async def test_successful_send_flips_exactly_the_emailed_jobs(digest_conn):
    """Precisely the included jobs, and nothing adjacent to them."""
    await digest_conn.add_job("a", title="Backend Engineer")
    await digest_conn.add_job("b", title="Platform Engineer")
    await digest_conn.add_job("skipped", title="Senior Backend Engineer")

    run = await run_digest(digest_conn, user_id=TEST_USER_ID, today=TODAY, tag_suffix="manual", sender=FakeResend())

    assert run.emailed == 2
    # The per-user table is what records a send, so it is what gets asserted.
    # Before Phase 1g this read the jobs.notified boolean, which said only that
    # SOMEBODY had been told.
    assert await digest_conn.notification_pairs() == {
        (TEST_USER_ID, "a"),
        (TEST_USER_ID, "b"),
    }
    assert await queue_ids(digest_conn) == {"skipped"}   # not sent, so still queued


async def test_successful_send_writes_one_notifications_row_per_job(digest_conn):
    await digest_conn.add_job("a", title="Backend Engineer")
    await digest_conn.add_job("b", title="Python Engineer")

    run = await run_digest(digest_conn, user_id=TEST_USER_ID, today=TODAY, tag_suffix="manual", sender=FakeResend())
    log = await digest_conn.sent_log()

    assert run.tag == "2026-07-26-manual"
    assert {row["canonical_id"] for row in log} == {"a", "b"}
    assert all(row["digest"] == "2026-07-26-manual" for row in log)
    assert all(row["sent_at"] for row in log)


async def test_a_second_run_after_a_successful_send_finds_nothing(digest_conn):
    """The exactly-once guarantee, proved by replay rather than by assertion."""
    await digest_conn.add_job("a", title="Backend Engineer")

    first = await run_digest(digest_conn, user_id=TEST_USER_ID, today=TODAY, sender=FakeResend())
    second_resend = FakeResend()
    second = await run_digest(digest_conn, user_id=TEST_USER_ID, today=TODAY, sender=second_resend)

    assert first.emailed == 1
    assert second.candidates == 0
    assert second_resend.calls == []
    assert len(await digest_conn.sent_log()) == 1


# --- the failed send ------------------------------------------------------


@pytest.mark.parametrize(
    "failure",
    [
        DigestError("Resend rejected the send (HTTP 403)"),
        DigestError("Resend request failed: ConnectTimeout"),
        RuntimeError("something unexpected"),
    ],
)
async def test_a_failed_send_changes_absolutely_nothing(digest_conn, failure):
    """Guard 1, applied to email: when the outcome is in doubt, write no RECEIPT.

    The alternative — flip first, send second — loses the job permanently if the
    send fails, and losing a job silently is the failure this table exists to
    prevent. A duplicate email is the cheaper mistake.

    "Nothing" is the lifecycle, not literally every column. The run does mark
    desc_checked on the way past, because reading a description happens before
    the send and what it learned is true whether or not an email went out. That
    write cannot make a job look delivered: no notification row, no receipt,
    nothing that would stop the next run retrying this exact set.
    """
    await digest_conn.add_job("a", title="Backend Engineer")
    await digest_conn.add_job("b", title="Platform Engineer")
    before = await lifecycle(digest_conn)

    with pytest.raises(type(failure)):
        await run_digest(digest_conn, user_id=TEST_USER_ID, today=TODAY, sender=FakeResend(fail=failure))

    assert await lifecycle(digest_conn) == before
    assert await digest_conn.sent_log() == []
    assert await queue_ids(digest_conn) == {"a", "b"}  # both still queued for a retry


async def test_the_run_after_a_failure_retries_the_same_jobs(digest_conn):
    await digest_conn.add_job("a", title="Backend Engineer")

    with pytest.raises(DigestError):
        await run_digest(digest_conn, user_id=TEST_USER_ID, today=TODAY, sender=FakeResend(fail=DigestError("503")))

    run = await run_digest(digest_conn, user_id=TEST_USER_ID, today=TODAY, sender=FakeResend())
    assert run.emailed == 1
    assert [row["canonical_id"] for row in await digest_conn.sent_log()] == ["a"]


async def test_record_sent_is_all_or_nothing(digest_conn):
    """A flag without a receipt would be a job we could never explain."""
    await digest_conn.add_job("a", title="Backend Engineer")

    with pytest.raises(Exception):
        async with digest_conn.transaction():
            await record_notified(digest_conn, TEST_USER_ID, ["a"])
            raise RuntimeError("insert blew up")

    assert await digest_conn.notification_pairs() == set()
    assert await digest_conn.sent_log() == []


async def test_record_sent_on_an_empty_list_is_a_no_op(digest_conn):
    await digest_conn.add_job("a", title="Backend Engineer")
    assert await record_sent(digest_conn, TEST_USER_ID, [], "2026-07-26-manual") == 0
    assert await digest_conn.sent_log() == []
    assert await digest_conn.notification_pairs() == set()


# --- the recipient assertion ---------------------------------------------


def test_sending_anywhere_but_my_email_is_refused_before_the_network():
    """Under the sandbox sender, which is what this install actually uses."""
    with pytest.raises(DigestError, match="can only deliver to the address"):
        assert_self_send("onboarding@resend.dev", "someone@else.test", "me@example.test")


def test_the_sandbox_sender_restriction_is_named_in_the_error():
    with pytest.raises(DigestError, match="sandbox sender"):
        assert_self_send("onboarding@resend.dev", "someone@else.test", "me@example.test")


def test_my_own_address_passes_regardless_of_case():
    assert_self_send("onboarding@resend.dev", "Me@Example.TEST", "me@example.test") is None


def test_a_verified_domain_may_send_to_someone_else():
    """The Phase-1e narrowing: the guard fires for its REASON, not as an absolute.

    The restriction has always been a property of onboarding@resend.dev — the
    error message said so before this changed. Stated as "only MY_EMAIL, ever",
    it would refuse every second user's digest permanently and verifying a domain
    would fix nothing, which turns a deliverability limit into a product one.

    This install still uses the sandbox sender, so nothing about today moves; the
    test above is the one describing the live configuration.
    """
    assert_self_send("digest@job-radar.test", "someone@else.test", "me@example.test") is None


async def test_run_digest_refuses_a_foreign_recipient_without_sending(digest_conn):
    """A mistyped recipient never reaches the network, and writes nothing."""
    await digest_conn.add_job("a", title="Backend Engineer")
    resend = FakeResend()

    with pytest.raises(DigestError, match="can only deliver to the address"):
        await run_digest(digest_conn, user_id=TEST_USER_ID, to="attacker@elsewhere.test", sender=resend)

    assert resend.calls == []
    assert await queue_ids(digest_conn) == {"a"}


# --- rendering ------------------------------------------------------------


def jobs_for_render() -> list[dict]:
    return [
        {"canonical_id": "1", "company": "Groww", "title": "Backend Engineer",
         "city": "blr", "location": "Bengaluru, India", "url": "https://g.test/1"},
        {"canonical_id": "2", "company": "Groww", "title": "Platform Engineer",
         "city": "blr", "location": "Bengaluru, India", "url": "https://g.test/2"},
        {"canonical_id": "3", "company": "Postman", "title": "API Engineer",
         "city": "india-unclear", "location": "Remote - India", "url": "https://p.test/3"},
    ]


def test_group_by_company_preserves_the_query_order():
    groups = group_by_company(jobs_for_render())
    assert list(groups) == ["Groww", "Postman"]
    assert [len(v) for v in groups.values()] == [2, 1]


def test_subject_line_leads_with_the_count_and_names_the_companies():
    assert subject_line(jobs_for_render(), TODAY) == (
        "job-radar: 3 new matches — Groww, Postman (2026-07-26)"
    )


def test_subject_line_is_singular_for_one_match():
    assert "1 new match —" in subject_line(jobs_for_render()[:1], TODAY)


def test_subject_line_summarises_a_long_company_list():
    jobs = [
        {**jobs_for_render()[0], "canonical_id": str(i), "company": f"Co{i}"} for i in range(6)
    ]
    assert subject_line(jobs, TODAY).endswith("Co0, Co1, Co2 +3 more (2026-07-26)")


def test_html_carries_every_job_with_its_link_city_and_raw_location():
    html = render_html(jobs_for_render(), Preferences(), TODAY)
    for job in jobs_for_render():
        assert job["title"] in html
        assert f'href="{job["url"]}"' in html
        assert job["location"] in html
    assert "Groww" in html and "Postman" in html
    assert "Bengaluru" in html and "India (unclear)" in html


def test_html_escapes_hostile_titles():
    """A recruiter's <script> is a string, not markup."""
    jobs = [{**jobs_for_render()[0], "title": '<script>alert("x")</script> Engineer'}]
    html = render_html(jobs, Preferences(), TODAY)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_html_is_self_contained_with_no_remote_assets():
    """Mail clients block remote content; nothing here needs fetching."""
    html = render_html(jobs_for_render(), Preferences(), TODAY)
    assert "<img" not in html
    assert "<style" not in html
    assert "<link" not in html


def test_plain_text_fallback_lists_every_job_and_its_url():
    text = render_text(jobs_for_render(), Preferences(), TODAY)
    for job in jobs_for_render():
        assert job["title"] in text
        assert job["url"] in text
    assert "Groww (2)" in text
    assert "Postman (1)" in text


async def test_the_sent_email_is_one_message_containing_both_bodies(digest_conn):
    await digest_conn.add_job("a", title="Backend Engineer")
    await digest_conn.add_job("b", title="Platform Engineer", company_id=1)

    resend = FakeResend()
    run = await run_digest(digest_conn, user_id=TEST_USER_ID, today=TODAY, sender=resend)

    assert len(resend.calls) == 1  # one batched digest, never one email per job
    call = resend.calls[0]
    assert call["subject"] == run.subject
    assert call["to"] == "me@example.test"
    assert call["from"] == "onboarding@resend.dev"
    assert "Backend Engineer" in call["html"] and "Backend Engineer" in call["text"]
    assert "Platform Engineer" in call["html"] and "Platform Engineer" in call["text"]
