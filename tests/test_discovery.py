"""The discovery sweep: what it tracks, what it refuses to track, and what it
refuses to touch.

Three properties are worth a test and the rest is reporting:

  1. The city bar is real. A board with jobs only in San Francisco is logged and
     NOT tracked, however alive it is — that bar is the only thing standing
     between "a sweep found 200 boards" and "the crawler now fetches 200 boards
     hourly for nothing".
  2. The sweep never clobbers. A company already in the table — hand-named,
     hand-categorised, or deliberately untracked — comes out of a second run
     exactly as it went in. This is asserted against the production statement,
     not against a paraphrase of it.
  3. The jobs table is not written. Ever. A company that is tracked here must
     still look brand-new to scripts/crawl.py, because that is what makes its
     back catalogue seed silently instead of arriving as email.

Everything here runs against the SQLite rig in conftest, which mirrors both
tables including their UNIQUE (ats, token) constraints — so ON CONFLICT has
something real to conflict against and idempotency is demonstrated rather than
asserted.
"""

from __future__ import annotations

import json

import pytest

from crawler.discover import (
    ALREADY,
    NO_CITY,
    TRACKED,
    UNRESOLVED,
    UNRESOLVED_ATS,
    Candidate,
    Known,
    Outcome,
    city_counts,
    city_hits,
    dedupe,
    load_known,
    log_row,
    looks_like_services,
    probe,
    record,
)
from fetchers import discovery as gh
from fetchers.resolve import DISCOVERY_ORDER, RESOLVE_ORDER
from scripts.discovery_seed import (
    ENTERPRISE_AND_GCC,
    PRODUCT_AND_SAAS,
    SEED_CANDIDATES,
    is_enterprise,
    load_seed_file,
)

from conftest import job


# --- the city bar ---------------------------------------------------------


def test_city_counts_uses_the_stored_labeller():
    jobs = [
        job("1", location="Bengaluru, India"),
        job("2", location="IND-BLR-Divyasree Technopolis"),
        job("3", location="Hyderabad, Telangana"),
        job("4", location="San Francisco, CA"),
        job("5", location="Remote"),
    ]
    counts = city_counts(jobs)
    assert counts == {"blr": 2, "hyd": 1, "other": 1, "india-unclear": 1}
    assert city_hits(counts) == ["blr", "hyd"]


def test_city_hits_order_is_stable_not_incidental():
    """Two cities in either input order produce the same recorded pair."""
    assert city_hits({"pune": 1, "blr": 3}) == ["blr", "pune"]
    assert city_hits({"blr": 3, "pune": 1}) == ["blr", "pune"]


def test_india_unclear_alone_does_not_clear_the_bar():
    """A board of "Remote" postings is not evidence of a Bangalore office.

    india-unclear keeps a JOB in the funnel — the bias described in
    fetchers/normalize.city_label — but it is not a city, and tracking a whole
    board on it would let any remote-only company in.
    """
    counts = city_counts([job("1", location="Remote"), job("2", location="Anywhere")])
    assert counts == {"india-unclear": 2}
    assert city_hits(counts) == []


async def test_board_with_no_target_city_is_logged_but_not_tracked(conn, monkeypatch):
    async def only_sf(ats, token, *, save_raw=True):
        return [job("1", location="San Francisco, CA")]

    monkeypatch.setattr("crawler.discover.fetch_tier1", only_sf)

    outcome = await record(
        conn, await probe(Candidate("Acme", ats="greenhouse", token="acme"))
    )

    assert outcome.verdict == NO_CITY
    assert await conn.fetchval("SELECT count(*) FROM companies") == 0
    row = (await conn.fetch("SELECT * FROM discovered_companies"))[0]
    assert (row["ats"], row["token"]) == ("greenhouse", "acme")
    assert row["promoted"] == 0
    assert json.loads(row["city_hits"]) == []


async def test_board_with_one_target_city_job_is_tracked(conn, monkeypatch):
    async def mixed(ats, token, *, save_raw=True):
        return [
            job("1", location="San Francisco, CA"),
            job("2", location="Pune, Maharashtra"),
        ]

    monkeypatch.setattr("crawler.discover.fetch_tier1", mixed)

    outcome = await record(
        conn, await probe(Candidate("Acme", ats="lever", token="acme"))
    )

    assert outcome.verdict == TRACKED
    assert outcome.hits == ["pune"]
    company = (await conn.fetch("SELECT * FROM companies"))[0]
    assert (company["name"], company["ats"], company["token"]) == ("Acme", "lever", "acme")
    assert company["tracked"] == 1
    assert company["category"] == "unknown"
    assert company["india_scoped"] == 0

    logged = (await conn.fetch("SELECT * FROM discovered_companies"))[0]
    assert logged["promoted"] == 1
    assert json.loads(logged["city_hits"]) == ["pune"]


async def test_empty_board_is_unresolved_not_tracked(conn, monkeypatch):
    """A 200 with no jobs proves nothing — Workable answers that for any name."""

    async def empty(ats, token, *, save_raw=True):
        return []

    monkeypatch.setattr("crawler.discover.fetch_tier1", empty)

    outcome = await record(
        conn, await probe(Candidate("Ghost", ats="workable", token="ghost"))
    )

    assert outcome.verdict == UNRESOLVED
    assert await conn.fetchval("SELECT count(*) FROM companies") == 0
    row = (await conn.fetch("SELECT * FROM discovered_companies"))[0]
    assert row["ats"] == UNRESOLVED_ATS


# --- never clobber --------------------------------------------------------


async def test_existing_company_is_left_exactly_as_it_was(conn, monkeypatch):
    """A hand-edited row survives a sweep that rediscovers the same board."""
    await conn.execute(
        "INSERT INTO companies (id, name, ats, token, config, category, tracked, india_scoped) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
        1, "Razorpay (India)", "greenhouse", "acme", '{"note": "hand-edited"}', "product", 1, 1,
    )
    before = dict((await conn.fetch("SELECT * FROM companies"))[0])

    async def in_blr(ats, token, *, save_raw=True):
        return [job("1", location="Bengaluru, India")]

    monkeypatch.setattr("crawler.discover.fetch_tier1", in_blr)
    outcome = await record(
        conn, await probe(Candidate("acme", ats="greenhouse", token="acme"))
    )

    assert outcome.verdict == ALREADY
    assert dict((await conn.fetch("SELECT * FROM companies"))[0]) == before
    assert await conn.fetchval("SELECT count(*) FROM companies") == 1


async def test_a_deliberately_untracked_company_is_not_re_tracked(conn, monkeypatch):
    """tracked = FALSE is a decision. A sweep must not overrule it.

    This is the case ON CONFLICT DO UPDATE would get wrong every single run:
    somebody turns a noisy board off, and the next discovery run turns it back
    on because the board still has Bangalore jobs — which was never in dispute.
    """
    await conn.add_company(1, "Noisy", ats="ashby", token="noisy", tracked=False)

    async def in_blr(ats, token, *, save_raw=True):
        return [job("1", location="Bengaluru, India")]

    monkeypatch.setattr("crawler.discover.fetch_tier1", in_blr)
    await record(conn, await probe(Candidate("noisy", ats="ashby", token="noisy")))

    assert await conn.fetchval("SELECT tracked FROM companies WHERE id = 1") == 0


async def test_running_twice_duplicates_nothing(conn, monkeypatch):
    async def in_blr(ats, token, *, save_raw=True):
        return [job("1", location="Bengaluru, India")]

    monkeypatch.setattr("crawler.discover.fetch_tier1", in_blr)
    candidate = Candidate("Acme", ats="greenhouse", token="acme")

    first = await record(conn, await probe(candidate))
    company_before = dict((await conn.fetch("SELECT * FROM companies"))[0])
    second = await record(conn, await probe(candidate))

    assert (first.verdict, second.verdict) == (TRACKED, ALREADY)
    assert await conn.fetchval("SELECT count(*) FROM companies") == 1
    assert await conn.fetchval("SELECT count(*) FROM discovered_companies") == 1
    assert dict((await conn.fetch("SELECT * FROM companies"))[0]) == company_before


async def test_promoted_is_never_taken_back(conn, monkeypatch):
    """A row promoted by hand stays promoted when its board goes quiet."""
    await conn.execute(
        "INSERT INTO discovered_companies (name, ats, token, promoted) VALUES ($1, $2, $3, $4)",
        "Acme", "greenhouse", "acme", 1,
    )

    async def only_sf(ats, token, *, save_raw=True):
        return [job("1", location="San Francisco, CA")]

    monkeypatch.setattr("crawler.discover.fetch_tier1", only_sf)
    outcome = await record(conn, await probe(Candidate("Acme", ats="greenhouse", token="acme")))

    assert outcome.verdict == NO_CITY
    assert await conn.fetchval("SELECT promoted FROM discovered_companies") == 1


# --- the jobs table is untouchable ----------------------------------------


async def test_no_statement_in_the_module_writes_jobs():
    """Read the SQL, not the intent. Neither statement names the jobs table.

    The silent seed depends on a freshly-tracked company having ZERO stored
    rows: scripts/crawl.py decides `seeding` from that count. A discovery run
    that inserted so much as one job row would flip that company to "already
    crawled" and its entire back catalogue would arrive as email.
    """
    from crawler import discover

    for statement in (discover.TRACK_SQL, discover.LOG_SQL):
        assert " jobs " not in statement.lower().replace("\n", " ")
        assert "into jobs" not in statement.lower()


async def test_a_tracked_company_still_looks_uncrawled(conn, monkeypatch):
    async def in_blr(ats, token, *, save_raw=True):
        return [job("1", location="Bengaluru, India"), job("2", location="Pune")]

    monkeypatch.setattr("crawler.discover.fetch_tier1", in_blr)
    await record(conn, await probe(Candidate("Acme", ats="greenhouse", token="acme")))

    from crawler.store import count_jobs

    company_id = await conn.fetchval("SELECT id FROM companies WHERE token = 'acme'")
    assert await count_jobs(conn, company_id) == 0  # -> seeding=True on the next crawl


# --- resume ----------------------------------------------------------------


async def test_known_skips_tracked_companies_and_logged_names(conn):
    await conn.add_company(1, "Razorpay", ats="greenhouse", token="razorpaysoftware")
    await conn.execute(
        "INSERT INTO discovered_companies (name, ats, token) VALUES ($1, $2, $3)",
        "Ghost Corp", UNRESOLVED_ATS, "ghostcorp",
    )
    known = await load_known(conn)

    assert known.seen(Candidate("razorpay")) == "already a tracked company"
    assert known.seen(Candidate("Razorpay Software")) is None  # a different name
    assert known.seen(Candidate("ghost corp")) is not None
    assert known.seen(Candidate("Never Seen")) is None


def test_known_matches_a_board_even_under_a_different_name():
    """The same board reached by two names is still one board."""
    known = Known(company_boards={("lever", "acme")})
    assert known.seen(Candidate("Acme Corp", ats="lever", token="acme")) is not None
    assert known.seen(Candidate("Acme Corp", ats="ashby", token="acme")) is None


def test_dedupe_keeps_the_typed_name_and_adopts_the_found_board():
    picked = dedupe(
        [
            Candidate("Acme", source="seed"),
            Candidate("acme", source="github", ats="lever", token="acme"),
            Candidate("Other", source="seed"),
        ]
    )
    assert [c.name for c in picked] == ["Acme", "Other"]
    assert (picked[0].ats, picked[0].token) == ("lever", "acme")
    assert picked[0].source == "seed+github"  # provenance of both halves


# --- the unresolved row ----------------------------------------------------


def test_unresolved_enterprise_is_logged_with_a_workday_note():
    outcome = Outcome(candidate=Candidate("IBM", enterprise=True), verdict=UNRESOLVED)
    outcome.note = "try Workday"
    name, ats, token, config, *_ = log_row(outcome)

    assert (name, ats, token) == ("IBM", UNRESOLVED_ATS, "ibm")
    assert json.loads(config)["note"] == "try Workday"


def test_unresolved_non_enterprise_gets_no_workday_note():
    name, ats, token, config, *_ = log_row(
        Outcome(candidate=Candidate("Tiny Startup"), verdict=UNRESOLVED)
    )
    assert token == "tinystartup"
    assert "note" not in json.loads(config)


def test_services_flag_is_a_hint_on_the_name():
    assert looks_like_services("Tata Consultancy Services")
    assert looks_like_services("Acme IT Services")
    assert looks_like_services("Bright Staffing")
    assert not looks_like_services("Razorpay")
    assert not looks_like_services("Quest Global")


# --- the seed list ---------------------------------------------------------


def test_seed_list_is_deduped_and_substantial():
    assert len(SEED_CANDIDATES) == len(set(SEED_CANDIDATES))
    assert len(SEED_CANDIDATES) >= 80


def test_every_supplied_tenant_is_in_the_seed_list():
    """The names handed over as confirmed park tenants must all be swept."""
    supplied = [
        "IBM", "Cognizant", "Nokia", "Rolls-Royce", "Philips", "AXA", "Target",
        "NXP Semiconductors", "GLOBALFOUNDRIES", "Harman", "Cerner",
        "Larsen & Toubro", "Justdial", "Intel", "Cisco", "HP", "Goldman Sachs",
    ]
    assert set(supplied) <= set(SEED_CANDIDATES)


def test_enterprise_membership_drives_the_workday_note():
    assert is_enterprise("IBM")
    assert is_enterprise("goldman sachs")  # case-insensitive
    assert not is_enterprise("Swiggy")
    assert not set(PRODUCT_AND_SAAS) & set(ENTERPRISE_AND_GCC)


def test_seed_file_ignores_comments_and_repeats(tmp_path):
    path = tmp_path / "extra.txt"
    path.write_text(
        "# a comment\n\n  Acme Corp  \nOther Inc  # trailing note\nAcme Corp\n",
        encoding="utf-8",
    )
    assert load_seed_file(path) == ["Acme Corp", "Other Inc"]


# --- resolve order ---------------------------------------------------------


def test_discovery_order_extends_the_dashboard_order_without_reordering_it():
    """The sweep may find MORE boards than "add by name", never a different one.

    RESOLVE_ORDER is ported to TypeScript for the dashboard, which has no Python
    to call. Keeping it as the prefix of the wide order is what stops a name
    resolving to greenhouse in one place and to workable in the other.
    """
    assert DISCOVERY_ORDER[: len(RESOLVE_ORDER)] == RESOLVE_ORDER
    assert [ats for ats, _ in DISCOVERY_ORDER] == [
        "greenhouse", "lever", "ashby", "smartrecruiters", "workable", "recruitee",
    ]


async def test_probe_survives_a_vendor_that_raises(monkeypatch):
    async def explode(ats, token, *, save_raw=True):
        raise RuntimeError("board on fire")

    monkeypatch.setattr("crawler.discover.fetch_tier1", explode)
    outcome = await probe(Candidate("Acme", ats="greenhouse", token="acme"))

    assert outcome.verdict == "failed"
    assert "board on fire" in outcome.error


# --- github mining ---------------------------------------------------------


def test_board_tokens_are_pulled_out_of_careers_urls():
    readme = """
    | Acme | https://boards.greenhouse.io/acme | open |
    | Beta | https://job-boards.greenhouse.io/betaco/jobs/4012 | |
    | Gamma | https://jobs.lever.co/gamma-corp/1234-5678 | |
    | Delta | https://jobs.ashbyhq.com/delta?utm_source=x | |
    | Embed | https://boards.greenhouse.io/embed/job_board?for=epsilon | |
    """
    assert gh.extract_boards(readme) == [
        ("greenhouse", "acme"),
        ("greenhouse", "betaco"),
        ("greenhouse", "epsilon"),
        ("lever", "gamma-corp"),
        ("ashby", "delta"),
    ]


def test_vendor_plumbing_is_not_mistaken_for_a_board():
    assert gh.extract_boards("https://boards.greenhouse.io/embed/job_board") == []
    assert gh.extract_boards("https://jobs.lever.co/api/postings") == []


def test_the_same_board_listed_twice_is_one_candidate():
    text = "jobs.lever.co/acme and later jobs.lever.co/acme again"
    assert gh.extract_boards(text) == [("lever", "acme")]


def test_workday_tenants_are_leads_not_boards():
    """A tenant is half a Workday config; the site name is not in the URL."""
    text = "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite"
    assert gh.extract_workday_tenants(text) == ["nvidia"]
    assert gh.extract_boards(text) == []


def test_mining_summary_counts_explicit_boards():
    line = gh.summarise(
        [
            gh.GithubCandidate("acme", "github", "lever", "acme"),
            gh.GithubCandidate("beta", "github", "lever", "beta"),
            gh.GithubCandidate("ibm", "github", note="try Workday"),
        ]
    )
    assert "3 candidate(s)" in line
    assert "2 with an explicit board" in line
    assert "lever=2" in line


@pytest.mark.parametrize("text", ["", None, "no urls here at all"])
def test_extraction_tolerates_junk(text):
    assert gh.extract_boards(text) == []
    assert gh.extract_workday_tenants(text) == []
