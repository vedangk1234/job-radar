"""The India verify sweep: what it measures, and what it is forbidden to touch.

This run exists to answer one question — of the harvested boards, how many hire
in India — and the danger of a measurement is that it quietly becomes an action.
So the properties worth a test are mostly about what does NOT happen:

  1. THE SEAL. One UPDATE, one table, keyed by id. It cannot insert a row, it
     cannot reach `companies` or `jobs`, and `promoted` does not appear in it.
     Asserted against the production statement TEXT, not a paraphrase, and again
     by counting rows either side of a sweep — the same two-way proof
     tests/test_commoncrawl.py gives the harvester.
  2. A DEAD BOARD IS WRITTEN AS 0, NOT NULL. This is the one line that made a
     separate module necessary: crawler.discover.log_row writes
     `jobs_seen or None`, and reusing it would make "probed and dead"
     indistinguishable from "never probed" — so every resume would re-fetch
     thousands of boards it had already paid for.
  3. THE HARVEST KEYS SURVIVE. A probe learns things about a board; it does not
     get to forget which crawl found it.
  4. THE LOOSE BAR IS THE LOOSE BAR, and it is measured twice. A board whose only
     India signal is `india-unclear` verdicts `india` (Vedang's call: store
     loosely) AND records india_named = 0, so the inflation is visible instead of
     baked in. The loose count is proved identical to counting
     fetchers.normalize.india_funnel rather than assumed to be.

Everything runs against the SQLite rig in conftest, which mirrors
discovered_companies including its UNIQUE (ats, token).
"""

from __future__ import annotations

import json

import pytest

from crawler.verify_india import (
    EMPTY,
    ERROR,
    INDIA,
    NO_INDIA,
    SR_MAX_PAGES,
    SR_PAGE_SIZE,
    VERIFY_SQL,
    Board,
    Verdict,
    commonest_unclear,
    probe_board,
    probe_config,
    record,
    verify_row,
)
from fetchers.normalize import india_funnel
from scripts.verify_india import LANE_WORKERS, build_lanes, interleave, parse_lane_delays

HARVEST_CONFIG = {
    "harvest": "commoncrawl",
    "crawl_id": "CC-MAIN-2026-30",
    "host": "job-boards.greenhouse.io",
}


def job(location, **extra):
    """A canonical-shaped job row, as fetchers/tier1.py hands them back."""
    return {"location": location, "title": "Engineer", "native_id": "1", **extra}


def board(**extra) -> Board:
    return Board(
        id=extra.pop("id", 1),
        ats=extra.pop("ats", "greenhouse"),
        token=extra.pop("token", "acme"),
        config=extra.pop("config", dict(HARVEST_CONFIG)),
    )


async def seed_harvested(conn, *, ats="greenhouse", token="acme", config=None) -> int:
    """One row exactly as scripts/harvest_commoncrawl.py leaves it: never probed."""
    await conn.execute(
        "INSERT INTO discovered_companies (name, ats, token, config, "
        "looks_like_services, source) VALUES ($1, $2, $3, $4::jsonb, $5, 'commoncrawl')",
        token,
        ats,
        token,
        json.dumps(config if config is not None else HARVEST_CONFIG),
        False,
    )
    return await conn.fetchval(
        "SELECT id FROM discovered_companies WHERE ats = $1 AND token = $2", ats, token
    )


# --- 1. the seal, against the statement text -------------------------------


def test_the_statement_names_only_discovered_companies():
    """`companies` and `jobs` may not be named at all.

    Same assertion tests/test_commoncrawl.py makes about the harvester, for the
    same reason: a run that writes `companies` promotes something, and a run that
    writes `jobs` destroys the first-ever-crawl detection that keeps a newly
    tracked company out of everybody's inbox.
    """
    sql = " ".join(VERIFY_SQL.lower().split())

    assert " jobs " not in sql
    assert "into jobs" not in sql
    # `discovered_companies` legitimately contains the substring "companies",
    # so remove it before asking whether the real table is named.
    assert "companies" not in sql.replace("discovered_companies", "")
    assert "update discovered_companies" in sql


def test_the_statement_cannot_create_or_promote_a_row():
    """UPDATE keyed by id: it can only fill in a row the harvester wrote."""
    sql = " ".join(VERIFY_SQL.lower().split())

    assert "insert" not in sql
    assert "delete" not in sql
    assert "on conflict" not in sql
    # THE scope fence, as a property of the statement rather than a promise.
    assert "promoted" not in sql
    assert "user_companies" not in sql
    assert "where id = $1" in sql


def executable_code(module) -> str:
    """A module's source with every comment and docstring removed.

    The prose in these two files talks about `companies`, `jobs` and `promoted`
    constantly — explaining what a run may not do is most of what the prose is
    for. What must not contain those words is the CODE, so the assertion is made
    against the code alone rather than against a file that explains itself.
    """
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path(module.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                node.body.pop(0)
    return ast.unparse(tree)  # unparse also drops every comment


def test_the_module_carries_no_promotion_path():
    """It cannot promote by accident: the words are not in the executable code."""
    import crawler.verify_india as module

    code = executable_code(module)
    for forbidden in ("TRACK_SQL", "INSERT INTO", "user_companies", "promoted"):
        assert forbidden not in code


# --- 1b. the seal, again, by counting rows ---------------------------------


async def test_a_verify_writes_only_discovered_companies(conn):
    await conn.execute(
        "INSERT INTO companies (name, ats, token, config, category, tracked, india_scoped) "
        "VALUES ($1, $2, $3, '{}', 'unknown', TRUE, FALSE)",
        "Existing",
        "greenhouse",
        "existing",
    )
    row_id = await seed_harvested(conn)
    before_companies = await conn.fetchval("SELECT count(*) FROM companies")
    before_jobs = await conn.fetchval("SELECT count(*) FROM jobs")
    before_rows = await conn.fetchval("SELECT count(*) FROM discovered_companies")

    await record(
        conn,
        Verdict(
            board=board(id=row_id),
            probe=INDIA,
            jobs=3,
            cities={"blr": 2, "non-india": 1},
        ),
    )

    assert await conn.fetchval("SELECT count(*) FROM companies") == before_companies
    assert await conn.fetchval("SELECT count(*) FROM jobs") == before_jobs
    # An UPDATE cannot add a row, and this proves it rather than assuming it.
    assert await conn.fetchval("SELECT count(*) FROM discovered_companies") == before_rows


async def test_a_verify_never_promotes(conn):
    """`promoted` comes out of a probe exactly as it went in, either way."""
    unpromoted = await seed_harvested(conn, token="quiet")
    promoted = await seed_harvested(conn, token="loud")
    await conn.execute(
        "UPDATE discovered_companies SET promoted = 1 WHERE id = $1", promoted
    )

    for row_id, jobs in ((unpromoted, 5), (promoted, 0)):
        await record(
            conn,
            Verdict(
                board=board(id=row_id),
                probe=INDIA if jobs else EMPTY,
                jobs=jobs,
                cities={"blr": jobs} if jobs else {},
            ),
        )

    assert await conn.fetchval(
        "SELECT promoted FROM discovered_companies WHERE id = $1", unpromoted
    ) in (0, False)
    assert await conn.fetchval(
        "SELECT promoted FROM discovered_companies WHERE id = $1", promoted
    ) in (1, True)


# --- 2. the three states: NULL, 0, and N -----------------------------------


async def test_a_dead_board_is_written_as_zero_not_null(conn):
    """The one line that made this a separate module from crawler.discover.

    `discover.log_row` writes `jobs_seen or None`. If this did the same, a board
    that answered with nothing would go back to job_count IS NULL — which is the
    resume predicate — and every re-run would re-fetch it forever.
    """
    row_id = await seed_harvested(conn, token="gone")

    await record(conn, Verdict(board=board(id=row_id, token="gone"), probe=EMPTY))

    stored = await conn.fetchval(
        "SELECT job_count FROM discovered_companies WHERE id = $1", row_id
    )
    assert stored == 0
    assert stored is not None


async def test_the_three_states_are_distinguishable(conn):
    """never probed / probed-and-dead / probed-with-jobs, by job_count alone."""
    never = await seed_harvested(conn, token="never")
    dead = await seed_harvested(conn, token="dead")
    alive = await seed_harvested(conn, token="alive")

    await record(conn, Verdict(board=board(id=dead, token="dead"), probe=EMPTY))
    await record(
        conn,
        Verdict(
            board=board(id=alive, token="alive"),
            probe=NO_INDIA,
            jobs=4,
            cities={"non-india": 4},
        ),
    )

    rows = {
        r["token"]: r["job_count"]
        for r in await conn.fetch("SELECT token, job_count FROM discovered_companies")
    }
    assert rows["never"] is None
    assert rows["dead"] == 0
    assert rows["alive"] == 4

    unprobed = await conn.fetch(
        "SELECT token FROM discovered_companies WHERE job_count IS NULL"
    )
    assert [r["token"] for r in unprobed] == ["never"]


async def test_an_errored_board_is_probed_and_not_retried(conn):
    """An error is recorded, carries its message, and leaves the resume pool."""
    row_id = await seed_harvested(conn, token="boom")

    await record(
        conn,
        Verdict(
            board=board(id=row_id, token="boom"),
            probe=ERROR,
            error="ReadTimeout: too slow",
        ),
    )

    job_count = await conn.fetchval(
        "SELECT job_count FROM discovered_companies WHERE id = $1", row_id
    )
    config = json.loads(
        await conn.fetchval(
            "SELECT config FROM discovered_companies WHERE id = $1", row_id
        )
    )
    assert job_count == 0  # not NULL: --retry-errors is how you ask again
    assert config["probe"] == ERROR
    assert config["probe_error"] == "ReadTimeout: too slow"


async def test_a_successful_retry_clears_the_old_error(conn):
    """A board that recovers must not keep a stale failure claiming it is broken."""
    row_id = await seed_harvested(conn, token="flaky")
    await record(
        conn,
        Verdict(board=board(id=row_id, token="flaky"), probe=ERROR, error="ConnectError: x"),
    )

    stored = json.loads(
        await conn.fetchval("SELECT config FROM discovered_companies WHERE id = $1", row_id)
    )
    await record(
        conn,
        Verdict(
            board=Board(id=row_id, ats="greenhouse", token="flaky", config=stored),
            probe=INDIA,
            jobs=2,
            cities={"blr": 2},
        ),
    )

    config = json.loads(
        await conn.fetchval("SELECT config FROM discovered_companies WHERE id = $1", row_id)
    )
    assert config["probe"] == INDIA
    assert "probe_error" not in config


# --- 3. the harvest keys survive -------------------------------------------


async def test_the_harvest_provenance_survives_a_probe(conn):
    """Which crawl found this board is not something a probe may forget."""
    config = dict(HARVEST_CONFIG, india_hint=True, india_hint_reason="token says india")
    row_id = await seed_harvested(conn, token="indiacorp", config=config)

    await record(
        conn,
        Verdict(
            board=Board(id=row_id, ats="greenhouse", token="indiacorp", config=config),
            probe=INDIA,
            jobs=7,
            cities={"blr": 5, "non-india": 2},
        ),
    )

    stored = json.loads(
        await conn.fetchval("SELECT config FROM discovered_companies WHERE id = $1", row_id)
    )
    assert stored["harvest"] == "commoncrawl"
    assert stored["crawl_id"] == "CC-MAIN-2026-30"
    assert stored["host"] == "job-boards.greenhouse.io"
    assert stored["india_hint"] is True
    assert stored["india_hint_reason"] == "token says india"
    # …alongside, not instead of, what the probe learned.
    assert stored["probe"] == INDIA
    assert stored["india_jobs"] == 5


# --- 4. the loose bar, and the number beside it ----------------------------


def test_a_board_that_is_only_unclear_still_clears_the_loose_bar():
    """Vedang's call: store loosely. `india-unclear` counts as India.

    This is the Dutch hotel, measured live before this module existed: ten jobs
    reading 'Eemnes, Utrecht, Nederland', ten india-unclear, and `nederland` is
    not in the non-India table while `netherlands` is.
    """
    verdict = Verdict(
        board=board(),
        probe=INDIA,
        jobs=10,
        cities={"india-unclear": 10},
    )

    assert verdict.india_loose == 10  # the stored bar says yes
    assert verdict.india_named == 0  # and the confident number says nothing
    assert verdict.india_unclear == 10


def test_the_two_numbers_split_a_mixed_board():
    verdict = Verdict(
        board=board(),
        probe=INDIA,
        jobs=20,
        cities={"blr": 4, "other-india": 2, "india-unclear": 5, "non-india": 9},
    )

    assert verdict.india_loose == 11  # 20 - 9 non-india
    assert verdict.india_named == 6  # blr + other-india: a real place, named
    assert verdict.india_unclear == 5
    assert verdict.non_india == 9


@pytest.mark.parametrize(
    "locations",
    [
        ["Bengaluru, India", "San Francisco, CA", "Remote"],
        ["Eemnes, Utrecht, Nederland", "London", ""],
        ["Amsterdam", "Berlin, Germany", "Tokyo"],
        ["Hyderabad", "Pune, Maharashtra", "Lucknow"],
    ],
)
def test_the_loose_bar_is_exactly_the_india_funnel_complement(locations):
    """Proved against the shared function rather than restated beside it.

    india_funnel is defined as "everything except non-india", and this sweep's
    loose count is `jobs - non_india`. If those two ever came apart, this run
    would be measuring a bar the rest of the project does not use.
    """
    from crawler.discover import city_counts

    jobs = [job(location) for location in locations]
    verdict = Verdict(board=board(), probe=INDIA, jobs=len(jobs), cities=city_counts(jobs))

    assert verdict.india_loose == sum(1 for location in locations if india_funnel(location))


def test_city_hits_records_every_india_label_and_never_non_india():
    verdict = Verdict(
        board=board(),
        probe=INDIA,
        jobs=6,
        cities={"blr": 2, "india-unclear": 1, "other-india": 2, "non-india": 1},
    )

    assert verdict.india_labels == ["blr", "india-unclear", "other-india"]
    assert "non-india" not in verdict.india_labels


async def test_city_hits_is_stored_widened_not_narrowed(conn):
    """The column used to mean 'which of blr/hyd/pune'; here it means 'which
    India labels'. Nothing outside crawler/discover.py and the tests reads it."""
    row_id = await seed_harvested(conn, token="wide")

    await record(
        conn,
        Verdict(
            board=board(id=row_id, token="wide"),
            probe=INDIA,
            jobs=3,
            cities={"mumbai": 1, "india-unclear": 2},
        ),
    )

    stored = await conn.fetchval(
        "SELECT city_hits FROM discovered_companies WHERE id = $1", row_id
    )
    assert json.loads(stored) == ["india-unclear", "mumbai"]


# --- the residue sample ----------------------------------------------------


def test_the_unclear_sample_is_the_commonest_unclear_string():
    jobs = [
        job("Eemnes, Utrecht, Nederland"),
        job("Eemnes, Utrecht, Nederland"),
        job("Remote"),
        job("Bengaluru, India"),
        job("San Francisco, CA"),
    ]
    assert commonest_unclear(jobs) == "Eemnes, Utrecht, Nederland"


def test_the_unclear_sample_is_none_when_nothing_was_unclear():
    assert commonest_unclear([job("Bengaluru, India"), job("Berlin, Germany")]) is None


def test_an_empty_location_is_recorded_readably():
    assert commonest_unclear([job(None), job("")]) == "<empty>"


# --- probe_board: the fetch, and the shapes it can come back in ------------


async def test_a_board_with_no_jobs_is_empty_not_an_error(monkeypatch):
    async def fake_fetch(ats, token, *, save_raw=True, max_pages=None):
        return []

    monkeypatch.setattr("crawler.verify_india.fetch_tier1", fake_fetch)
    verdict = await probe_board(board())

    assert verdict.probe == EMPTY
    assert verdict.jobs == 0
    assert verdict.india_loose == 0


async def test_a_fetch_that_raises_is_recorded_not_propagated(monkeypatch):
    """One unreachable vendor must not end a sweep of eleven thousand boards."""

    async def fake_fetch(ats, token, *, save_raw=True, max_pages=None):
        raise RuntimeError("vendor exploded")

    monkeypatch.setattr("crawler.verify_india.fetch_tier1", fake_fetch)
    verdict = await probe_board(board())

    assert verdict.probe == ERROR
    assert verdict.error == "RuntimeError: vendor exploded"
    assert verdict.jobs == 0


async def test_a_board_with_only_foreign_jobs_is_no_india(monkeypatch):
    async def fake_fetch(ats, token, *, save_raw=True, max_pages=None):
        return [job("San Francisco, CA"), job("Berlin, Germany")]

    monkeypatch.setattr("crawler.verify_india.fetch_tier1", fake_fetch)
    verdict = await probe_board(board())

    assert verdict.probe == NO_INDIA
    assert verdict.india_loose == 0
    assert verdict.jobs == 2


async def test_the_probe_never_saves_a_fixture(monkeypatch):
    """These are probes over eleven thousand boards; the archive is not for them."""
    seen = {}

    async def fake_fetch(ats, token, *, save_raw=True, max_pages=None):
        seen["save_raw"] = save_raw
        return [job("Bengaluru, India")]

    monkeypatch.setattr("crawler.verify_india.fetch_tier1", fake_fetch)
    await probe_board(board())

    assert seen["save_raw"] is False


async def test_the_smartrecruiters_cap_is_passed_and_truncation_flagged(monkeypatch):
    """A capped board is flagged, so its count is never silently understated."""

    async def fake_fetch(ats, token, *, save_raw=True, max_pages=None):
        assert max_pages == 3
        return [job("Bengaluru, India")] * (3 * SR_PAGE_SIZE)

    monkeypatch.setattr("crawler.verify_india.fetch_tier1", fake_fetch)
    verdict = await probe_board(
        board(ats="smartrecruiters", token="aecom2"), sr_max_pages=3
    )

    assert verdict.truncated is True
    assert probe_config(verdict)["truncated"] is True


async def test_a_short_board_is_not_flagged_truncated(monkeypatch):
    async def fake_fetch(ats, token, *, save_raw=True, max_pages=None):
        return [job("Bengaluru, India")] * 42

    monkeypatch.setattr("crawler.verify_india.fetch_tier1", fake_fetch)
    verdict = await probe_board(board(ats="smartrecruiters"), sr_max_pages=3)

    assert verdict.truncated is False
    assert "truncated" not in probe_config(verdict)


def test_the_default_cap_is_far_past_the_top_bucket():
    """20+ is the last distribution bucket; 10 pages is 1,000 jobs."""
    assert SR_MAX_PAGES * SR_PAGE_SIZE >= 1000


# --- the fetcher kwarg is additive -----------------------------------------


async def test_only_the_paginated_fetcher_receives_max_pages(monkeypatch):
    """A caller sweeping a mixed list sets it once without knowing which vendor
    paginates — and the five that do not must not be handed an unknown kwarg."""
    import fetchers.tier1 as tier1

    calls = {}

    async def fake_greenhouse(token, *, save_raw=True):
        calls["greenhouse"] = "no max_pages, and it did not raise"
        return []

    async def fake_sr(token, *, save_raw=True, max_pages=tier1.SR_MAX_PAGES):
        calls["smartrecruiters"] = max_pages
        return []

    monkeypatch.setitem(tier1.FETCHERS, "greenhouse", fake_greenhouse)
    monkeypatch.setitem(tier1.FETCHERS, "smartrecruiters", fake_sr)
    monkeypatch.setattr(tier1, "fetch_smartrecruiters", fake_sr)

    await tier1.fetch("greenhouse", "acme", max_pages=7)
    await tier1.fetch("smartrecruiters", "acme", max_pages=7)

    assert calls["greenhouse"]
    assert calls["smartrecruiters"] == 7


def test_the_default_page_cap_is_unchanged_for_every_existing_caller():
    import fetchers.tier1 as tier1

    assert tier1.SR_MAX_PAGES == 50


# --- the sweep: lanes, ordering, politeness --------------------------------


def test_the_worker_allocation_matches_the_semaphore():
    """The starvation-free invariant, asserted rather than commented.

    http.py takes a semaphore slot BEFORE waiting out a host's pace, so a worker
    queued on a host lock is holding a slot. Total workers == MAX_CONCURRENCY is
    what guarantees no worker ever blocks another lane.
    """
    from fetchers.http import MAX_CONCURRENCY

    assert sum(LANE_WORKERS.values()) == MAX_CONCURRENCY


def test_every_lane_is_a_harvestable_ats():
    from fetchers.commoncrawl import TARGETS

    harvestable = {t.ats for t in TARGETS if not t.expect_empty}
    assert set(LANE_WORKERS) == harvestable


def test_interleave_spreads_a_limit_across_every_lane():
    """`--limit 300` must calibrate all five lanes, not 300 Greenhouse boards."""
    boards = (
        [Board(id=i, ats="greenhouse", token=f"g{i}") for i in range(100)]
        + [Board(id=200 + i, ats="ashby", token=f"a{i}") for i in range(50)]
        + [Board(id=400 + i, ats="recruitee", token=f"r{i}") for i in range(10)]
    )

    first_thirty = interleave(boards)[:30]

    assert {b.ats for b in first_thirty} == {"greenhouse", "ashby", "recruitee"}
    assert len(first_thirty) == 30


def test_interleave_loses_nothing():
    boards = [Board(id=i, ats="greenhouse" if i % 3 else "ashby", token=str(i)) for i in range(37)]
    assert sorted(b.id for b in interleave(boards)) == sorted(b.id for b in boards)


def test_lanes_carry_their_worker_count_and_delay():
    boards = [
        Board(id=1, ats="greenhouse", token="a"),
        Board(id=2, ats="recruitee", token="b"),
    ]
    lanes = build_lanes(boards, {"greenhouse": 2.5})

    assert lanes["greenhouse"].workers == LANE_WORKERS["greenhouse"]
    assert lanes["greenhouse"].extra_delay == 2.5
    assert lanes["recruitee"].extra_delay == 0.0


def test_lane_delay_parsing_refuses_an_unknown_vendor():
    assert parse_lane_delays(["workable=2.0"]) == {"workable": 2.0}
    with pytest.raises(SystemExit):
        parse_lane_delays(["greenhous=2.0"])


def test_the_resume_predicate_is_the_never_probed_one():
    from crawler.verify_india import SELECT_UNPROBED_SQL

    sql = " ".join(SELECT_UNPROBED_SQL.lower().split())
    assert "job_count is null" in sql
    assert "source = $1" in sql


def test_the_sweep_never_writes_a_second_table():
    """The script around the statement must not smuggle one in.

    It READS `companies` and `jobs` to print the seal, which is the whole point
    of the seal, so this is about writing them rather than about naming them.
    """
    import scripts.verify_india as sweep

    code = executable_code(sweep)
    for forbidden in (
        "INSERT INTO",
        "UPDATE companies",
        "DELETE FROM",
        "TRACK_SQL",
        "user_companies",
    ):
        assert forbidden not in code
    for read_only in ("SELECT count(*) FROM companies", "SELECT count(*) FROM jobs"):
        assert read_only in code


def test_the_row_tuple_is_the_statements_five_parameters():
    verdict = Verdict(
        board=board(id=99), probe=INDIA, jobs=3, cities={"blr": 3}
    )
    row = verify_row(verdict)

    assert len(row) == 5
    assert row[0] == 99  # $1 id
    assert row[1] == 3  # $2 job_count
    assert row[2] == ["blr"]  # $3 city_hits
    assert json.loads(row[4])["probe"] == INDIA  # $5 config
