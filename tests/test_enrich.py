"""The crawl's enrichment pass: the hole it closes, and the ones it must not open.

The hole, stated once: a job that has never been emailed was never enriched,
because enrichment ran over the digest's own send queue. The dashboard
does not read that queue — it shows everything open that fits — so a freshly
crawled job appeared there with nothing known about its experience requirement,
and "nothing known" means KEEP everywhere in this project. A real Adobe posting
asking for 4-6 years was visible for exactly that reason.

Every test below is about one of four things:

    it reads the descriptions of the jobs that would be SHOWN, not the ones
        that would be SENT                                    (the fix)
    it reads only the jobs worth a request                    (the cost)
    it cannot disturb applied/seeded/closed state             (the risk)
    it reads for EVERY active account, and asks nobody who is running it
                                                              (Phase 2b)

The last group is the second half of the same hole. Until 2b this pass resolved
"the current user" from MY_EMAIL and narrowed itself to that one person's filter,
so a second account whose keywords differ had their matches skipped here — and an
unread description means min_experience NULL, and NULL means KEEP, so those jobs
reached that person's digest on no evidence at all. Reading is user-blind work
(the two facts land on the job, one fetch serves everybody), so the pass now runs
over the union of every active filter and resolves no user.

No network anywhere: the fetcher is injected, exactly as in test_description.py.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from crawler.enrich import RETRY_AFTER, enrich_matches
from notify.match import Preferences, is_match
from tests.conftest import TEST_USER_ID, USER_B_EMAIL, USER_B_ID

# Noon on the day the FakeConn clock starts, so a timestamp the shim's own
# `now()` wrote during a test is a few minutes BEFORE this rather than three
# weeks after it. Getting that backwards makes every freshly written row look
# stale and quietly turns the retry tests into no-ops.
NOW = datetime(2026, 7, 1, 12, tzinfo=timezone.utc)
HALF_AN_HOUR_LATER = NOW.replace(minute=30)
A_DAY_LATER = NOW + RETRY_AFTER * 2
AN_HOUR_AGO = (NOW - RETRY_AFTER / 24).isoformat()

# The real Adobe requirement line, out of the real posting.
ADOBE = "4–6 years of experience in software engineering."
JUNIOR = "1-2 years of experience building web services."


def fetcher_for(texts: dict[str, str | None]):
    """A fetch_description that answers from a dict, and counts what it was asked."""
    asked: list[str] = []

    async def fetch(company, job):
        asked.append(job["canonical_id"])
        return texts.get(job["canonical_id"])

    return fetch, asked


async def test_a_matching_job_nobody_has_emailed_still_gets_its_description_read(digest_conn):
    """THE BUG, as a test.

    `seeded` is TRUE here, which is what a silently-seeded job looks like and
    what every row on this table was. The old enrichment ran over the digest's
    own queue and would have skipped it entirely; Home would still have shown it,
    because Home is not the outbox.
    """
    await digest_conn.add_job(
        "adobe-fullstack",
        title="Computer Scientist - Fullstack",
        location="Bangalore, India",
        seeded=True,
    )

    fetch, asked = fetcher_for({"adobe-fullstack": ADOBE})
    result = await enrich_matches(digest_conn, fetcher=fetch, now=NOW)

    assert asked == ["adobe-fullstack"]
    row = (await digest_conn.snapshot())["adobe-fullstack"]
    assert row["min_experience"] == 4
    assert row["desc_checked"] == 1
    assert result.read == 1
    assert [j["canonical_id"] for j in result.hidden] == ["adobe-fullstack"]


async def test_the_adobe_posting_stops_matching_once_its_description_is_read(digest_conn):
    """Before: a match on every test that had been run. After: 4 years, hidden.

    Both halves are asserted, because "it does not match now" is only meaningful
    alongside "it did match before" — that gap is precisely what was on screen.
    """
    await digest_conn.add_job(
        "adobe-fullstack", title="Computer Scientist - Fullstack", location="Bangalore, India"
    )

    from notify.store import fetch_candidates

    before = (await fetch_candidates(digest_conn, TEST_USER_ID))[0]
    assert is_match(before, Preferences())

    fetch, _ = fetcher_for({"adobe-fullstack": ADOBE})
    await enrich_matches(digest_conn, fetcher=fetch, now=NOW)

    after = (await fetch_candidates(digest_conn, TEST_USER_ID))[0]
    assert after["min_experience"] == 4
    assert not is_match(after, Preferences())


async def test_a_job_stating_no_experience_stays_visible(digest_conn):
    """The inclusion bias, after a successful read rather than before one.

    This is the case the whole filter is built around not breaking: silence in a
    description is not a requirement, and a detector that guessed would delete
    the majority of real postings.
    """
    await digest_conn.add_job("quiet", title="Backend Engineer", location="Bengaluru, India")

    fetch, _ = fetcher_for({"quiet": "Build and operate our payments platform. Strong fundamentals."})
    result = await enrich_matches(digest_conn, fetcher=fetch, now=NOW)

    row = (await digest_conn.snapshot())["quiet"]
    assert row["min_experience"] is None
    assert row["desc_checked"] == 1
    assert row["desc_fetch_failed"] == 0
    assert result.hidden == []
    assert is_match(dict(row), Preferences())


async def test_only_title_stage_matches_are_ever_fetched(digest_conn):
    """The cost argument, asserted rather than assumed.

    788 open rows, 36 title-stage matches on the live table. A pass that fetched
    the whole board would be a second request per job per crawl forever, and
    would answer a question that only matters for the handful that already
    passed every free test.
    """
    await digest_conn.add_job("keep", title="Backend Engineer", location="Bengaluru, India")
    await digest_conn.add_job("senior", title="Senior Backend Engineer", location="Bengaluru, India")
    await digest_conn.add_job("level", title="SDE-4", location="Bengaluru, India")
    await digest_conn.add_job("elsewhere", title="Backend Engineer", location="Berlin, Germany")
    await digest_conn.add_job("unrelated", title="Account Executive", location="Bengaluru, India")

    fetch, asked = fetcher_for({})
    result = await enrich_matches(digest_conn, fetcher=fetch, now=NOW)

    assert asked == ["keep"]
    assert result.considered == 5
    assert result.title_stage == 1


async def test_a_closed_job_is_never_worth_a_request(digest_conn):
    """Politeness spent on a posting nobody can apply to is politeness wasted."""
    await digest_conn.add_job(
        "gone", title="Backend Engineer", location="Bengaluru, India", status="closed"
    )

    fetch, asked = fetcher_for({"gone": ADOBE})
    result = await enrich_matches(digest_conn, fetcher=fetch, now=NOW)

    assert asked == []
    assert result.considered == 0


async def test_a_description_already_read_is_never_read_again(digest_conn):
    """desc_checked is the gate, and it is the only thing making this cheap."""
    await digest_conn.add_job(
        "known",
        title="Backend Engineer",
        location="Bengaluru, India",
        min_experience=2,
        desc_checked=True,
        desc_checked_at=AN_HOUR_AGO,
    )

    fetch, asked = fetcher_for({"known": ADOBE})
    result = await enrich_matches(digest_conn, fetcher=fetch, now=NOW)

    assert asked == []
    assert result.checked == 0
    assert (await digest_conn.snapshot())["known"]["min_experience"] == 2


async def test_a_board_that_answered_nothing_is_kept_and_asked_again_tomorrow(digest_conn):
    """Two runs. The first learns nothing; the second, a day later, learns 4.

    A failure that was believed forever is how the Workday rows on the live
    table ended up marked clean without ever having been read.
    """
    await digest_conn.add_job(
        "flaky", title="Computer Scientist - Fullstack", location="Bangalore, India"
    )

    dead, asked_first = fetcher_for({})
    first = await enrich_matches(digest_conn, fetcher=dead, now=NOW)

    assert asked_first == ["flaky"]
    assert first.failed == 1
    row = (await digest_conn.snapshot())["flaky"]
    assert row["desc_checked"] == 1 and row["desc_fetch_failed"] == 1
    assert is_match(dict(row), Preferences())  # unknown, therefore still shown

    # Half an hour later — the next crawl — nothing is re-asked.
    alive, asked_soon = fetcher_for({"flaky": ADOBE})
    await enrich_matches(digest_conn, fetcher=alive, now=HALF_AN_HOUR_LATER)
    assert asked_soon == []

    # A day later it is.
    alive, asked_later = fetcher_for({"flaky": ADOBE})
    later = await enrich_matches(digest_conn, fetcher=alive, now=A_DAY_LATER)

    assert asked_later == ["flaky"]
    assert later.read == 1
    row = (await digest_conn.snapshot())["flaky"]
    assert row["min_experience"] == 4
    assert row["desc_fetch_failed"] == 0
    assert not is_match(dict(row), Preferences())


async def test_a_pm_role_is_hidden_by_the_same_pass(digest_conn):
    """The other description-derived exclusion, on the same evidence."""
    await digest_conn.add_job("builder", title="Full Stack Builder", location="Bengaluru, India")

    fetch, _ = fetcher_for({"builder": "You will be the product owner for checkout."})
    result = await enrich_matches(digest_conn, fetcher=fetch, now=NOW)

    assert (await digest_conn.snapshot())["builder"]["is_pm_role"] == 1
    assert [j["title"] for j in result.hidden] == ["Full Stack Builder"]


# --- what enrichment must not be able to do -------------------------------


async def lifecycle(conn) -> dict[str, tuple]:
    """The columns a crawl owns. Enrichment may not move any of them."""
    rows = await conn.snapshot()
    return {
        cid: (row["seeded"], row["status"], row["miss_count"], row["first_seen_at"])
        for cid, row in rows.items()
    }


async def test_enrichment_disturbs_no_lifecycle_column(digest_conn):
    """applied/seeded/closed state, before and after, column by column.

    This runs in the middle of a crawl now, which is the one place a stray
    UPDATE would be most expensive: re-opening a closed job or clearing the
    `seeded` marker would re-alert a board, and re-alerting a board is the
    failure the whole diffing engine exists to prevent.
    """
    await digest_conn.add_job("fresh", title="Backend Engineer", location="Bengaluru, India")
    await digest_conn.add_job(
        "sent", title="Platform Engineer", location="Bengaluru, India", seeded=True
    )
    await digest_conn.add_job(
        "shut", title="Frontend Engineer", location="Bengaluru, India", status="closed"
    )

    before = await lifecycle(digest_conn)

    fetch, _ = fetcher_for({"fresh": ADOBE, "sent": JUNIOR, "shut": ADOBE})
    await enrich_matches(digest_conn, fetcher=fetch, now=NOW)

    assert await lifecycle(digest_conn) == before
    assert await digest_conn.sent_log() == []


async def test_no_column_anywhere_holds_the_description_text(digest_conn):
    """The promise db/schema.sql makes, checked against the actual table."""
    await digest_conn.add_job("adobe", title="Computer Scientist - Fullstack", location="Bangalore, India")

    fetch, _ = fetcher_for({"adobe": ADOBE})
    await enrich_matches(digest_conn, fetcher=fetch, now=NOW)

    for row in (await digest_conn.snapshot()).values():
        for column, value in row.items():
            assert "software engineering" not in str(value), column


@pytest.mark.parametrize("runs", [1, 2, 3])
async def test_the_pass_is_idempotent_within_a_crawl_cycle(digest_conn, runs):
    """Crawls every thirty minutes; descriptions read once. Not once per crawl."""
    await digest_conn.add_job("job", title="Backend Engineer", location="Bengaluru, India")

    fetch, asked = fetcher_for({"job": JUNIOR})
    for _ in range(runs):
        await enrich_matches(digest_conn, fetcher=fetch, now=NOW)

    assert asked == ["job"]


# --- whose filter decides (Phase 2b: nobody's in particular) ---------------
#
# A second account with a genuinely different filter, so "the union" is a claim
# with a way to be false. Both users are is_active — the fan-out's own
# definition of who counts, read through the same column.


async def a_second_user_wanting(conn, **prefs) -> None:
    """User B exists, is active, and has their own filter."""
    await conn.add_user(USER_B_ID, USER_B_EMAIL)
    await conn.add_preferences_for(USER_B_ID, **prefs)


async def test_a_job_only_the_second_users_filter_wants_is_still_enriched(digest_conn):
    """THE 2b BUG, as a test.

    User A watches for backend roles in Bengaluru; user B for machine-learning
    roles in Pune. The ML job in Pune is invisible to A's filter, which is the
    only one the old pass could see — so its description went unread, its
    min_experience stayed NULL, and B was emailed a job that turns out to want
    four to six years. Enriching it here is the whole point of the union.

    A's filter is narrowed to Bengaluru backend roles rather than left on the
    seeded defaults, because those cover three cities and machine learning among
    their keywords — so the ML job would have been fetched for A's sake and the
    test would prove nothing about B.
    """
    await digest_conn.set_preferences(
        cities=["blr"], keywords=["backend"], exclusions=["senior"]
    )
    await a_second_user_wanting(
        digest_conn, cities=["pune"], keywords=["machine learning"], exclusions=["senior"]
    )
    await digest_conn.add_job(
        "b-ml", title="Machine Learning Engineer", location="Pune, India"
    )
    await digest_conn.add_job(
        "a-backend", title="Backend Engineer", location="Bengaluru, India"
    )

    fetch, asked = fetcher_for({"b-ml": ADOBE, "a-backend": JUNIOR})
    result = await enrich_matches(digest_conn, fetcher=fetch, now=NOW)

    assert sorted(asked) == ["a-backend", "b-ml"]
    assert result.title_stage == 2
    assert (await digest_conn.snapshot())["b-ml"]["min_experience"] == 4


async def test_a_job_no_active_filter_wants_is_still_not_worth_a_request(digest_conn):
    """The union widens the pass; it does not remove the seam.

    Two filters between them are still two filters, not "everything open" — the
    cost argument in test_only_title_stage_matches_are_ever_fetched survives a
    second account, and a pass that quietly stopped narrowing would fetch a
    description for every row on the board.
    """
    await a_second_user_wanting(
        digest_conn, cities=["pune"], keywords=["machine learning"], exclusions=["senior"]
    )
    await digest_conn.add_job("sales", title="Account Executive", location="Pune, India")
    await digest_conn.add_job(
        "elsewhere", title="Machine Learning Engineer", location="Berlin, Germany"
    )
    await digest_conn.add_job("senior", title="Senior Backend Engineer", location="Bengaluru, India")

    fetch, asked = fetcher_for({})
    result = await enrich_matches(digest_conn, fetcher=fetch, now=NOW)

    assert asked == []
    assert result.considered == 3
    assert result.title_stage == 0


async def test_a_deactivated_account_does_not_earn_anybody_a_request(digest_conn):
    """is_active is the fan-out's off switch, and this reads the same column.

    A deactivated account keeps its filter and stops receiving email. Spending
    politeness on descriptions for a filter nobody is being sent would be the
    crawl doing work on behalf of somebody who has switched the product off.

    User A is narrowed to Bengaluru backend roles first, so the ML job in Pune is
    wanted by B's filter and by nothing else — otherwise the seeded defaults
    (three cities, machine learning among the keywords) would fetch it anyway and
    the test would pass without B's row having any say.
    """
    await digest_conn.set_preferences(
        cities=["blr"], keywords=["backend"], exclusions=["senior"]
    )
    await a_second_user_wanting(
        digest_conn, cities=["pune"], keywords=["machine learning"], exclusions=["senior"]
    )
    await digest_conn.execute("UPDATE users SET is_active = FALSE WHERE id = $1", USER_B_ID)
    await digest_conn.add_job(
        "b-ml", title="Machine Learning Engineer", location="Pune, India"
    )

    fetch, asked = fetcher_for({"b-ml": ADOBE})
    result = await enrich_matches(digest_conn, fetcher=fetch, now=NOW)

    assert asked == []
    assert result.title_stage == 0


async def test_a_job_hidden_for_one_person_and_kept_for_another_is_not_reported_hidden(
    digest_conn,
):
    """`hidden` is printed once per crawl, so it has to mean "hidden for nobody".

    Both users want ML roles in Pune; only A excludes the word "staff". The
    description says nothing disqualifying, so the row survives for B — and
    reporting it as newly hidden would describe a screen that still shows it.
    """
    await digest_conn.set_preferences(
        cities=["pune"], keywords=["machine learning"], exclusions=["staff"]
    )
    await a_second_user_wanting(
        digest_conn, cities=["pune"], keywords=["machine learning"], exclusions=["senior"]
    )
    await digest_conn.add_job(
        "staff-ml", title="Staff Machine Learning Engineer", location="Pune, India"
    )

    fetch, asked = fetcher_for({"staff-ml": JUNIOR})
    result = await enrich_matches(digest_conn, fetcher=fetch, now=NOW)

    assert asked == ["staff-ml"]           # B's filter wanted it read
    assert result.hidden == []             # and B can still see it


async def test_the_pass_runs_with_MY_EMAIL_unset(digest_conn, monkeypatch):
    """The bridge is gone from the crawl path, proved by taking the variable away.

    The strongest form of "identity no longer comes from the environment": delete
    the variable the old lookup read and the pass has to be indifferent to it. It
    fails loudly if anything on this path ever reaches for it again — a textual
    assertion (tests/test_identity_bridge.py) can be satisfied by a new name for
    the same habit, and this cannot.
    """
    monkeypatch.delenv("MY_EMAIL", raising=False)
    await digest_conn.add_job("adobe", title="Backend Engineer", location="Bengaluru, India")

    fetch, asked = fetcher_for({"adobe": ADOBE})
    result = await enrich_matches(digest_conn, fetcher=fetch, now=NOW)

    assert asked == ["adobe"]
    assert result.read == 1


async def test_an_unowned_preferences_row_stops_the_pass_rather_than_widening_it(
    digest_conn,
):
    """The half-applied migration, which the union must not paper over.

    db/schema.sql applied and scripts/backfill_preferences_user.py not yet run
    leaves a real tuned filter sitting there with no owner, invisible to a JOIN on
    public.users. Falling back to the seeded defaults there would enrich against a
    filter nobody chose — the same silence notify/store.load_preferences refuses on
    the digest side, refused here for the same reason.
    """
    await digest_conn.set_preferences(cities=["blr"], keywords=["backend"], user_id=None)
    await digest_conn.add_job("job", title="Backend Engineer", location="Bengaluru, India")

    fetch, asked = fetcher_for({"job": ADOBE})
    with pytest.raises(RuntimeError, match="backfill_preferences_user"):
        await enrich_matches(digest_conn, fetcher=fetch, now=NOW)
    assert asked == []
