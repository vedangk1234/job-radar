"""The crawl's enrichment pass: the hole it closes, and the ones it must not open.

The hole, stated once: a job that has never been emailed was never enriched,
because enrichment ran over the digest's own send queue. The dashboard
does not read that queue — it shows everything open that fits — so a freshly
crawled job appeared there with nothing known about its experience requirement,
and "nothing known" means KEEP everywhere in this project. A real Adobe posting
asking for 4-6 years was visible for exactly that reason.

Every test below is about one of three things:

    it reads the descriptions of the jobs that would be SHOWN, not the ones
        that would be SENT                                    (the fix)
    it reads only the jobs worth a request                    (the cost)
    it cannot disturb applied/seeded/closed state             (the risk)

No network anywhere: the fetcher is injected, exactly as in test_description.py.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from crawler.enrich import RETRY_AFTER, enrich_matches
from notify.match import Preferences, is_match
from tests.conftest import TEST_USER_ID

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
