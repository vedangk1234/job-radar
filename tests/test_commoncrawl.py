"""The Common Crawl harvester: what it extracts, what it skips, what it seals.

Four properties are worth a test and the rest is reporting:

  1. The token that comes out of a CDX URL is BYTE-IDENTICAL to the one
     fetchers/discovery.py lifts out of a README careers link. The dedupe key is
     (ats, token); a token that normalised differently here would be stored a
     second time and re-probed forever.
  2. The harvester writes discovered_companies and nothing else. Asserted
     against the production statement TEXT and again by counting rows, the same
     way tests/test_discovery.py proves the sweep never writes jobs.
  3. An already-known board is skipped without a write, so a re-run is free.
  4. The India hint is a hint. It fires on an anchored marker, does NOT fire on
     the "desi" inside "design", and gates nothing — a token it does not flag is
     stored exactly like one it does.

Everything database-facing runs against the SQLite rig in conftest, which
mirrors discovered_companies including its UNIQUE (ats, token) — so ON CONFLICT
DO NOTHING has something real to conflict against.

No test here touches the network. The CDX index is stubbed at `polite_get`.
"""

from __future__ import annotations

import json

import pytest

from fetchers import commoncrawl as cc
from fetchers import discovery as gh
from fetchers.commoncrawl import (
    TARGETS,
    Target,
    india_hint,
    token_from_url,
    tokens_from_urls,
)
from scripts.harvest_commoncrawl import HARVEST_SQL, harvest_row

GREENHOUSE_NEW = TARGETS[0]
GREENHOUSE_OLD = TARGETS[1]
ASHBY = TARGETS[2]
SMARTRECRUITERS = TARGETS[3]
WORKABLE = TARGETS[4]
RECRUITEE = TARGETS[5]
LEVER = TARGETS[6]


class _FakeFetched:
    """What polite_get hands back. CDX bodies are text, never parsed JSON."""

    def __init__(self, text: str = "", *, ok: bool = True, status: int = 200):
        self.ok = ok
        self.status = status
        self.text = text
        self.json = None
        self.error = None if ok else f"HTTP {status}"


def cdx_lines(*urls: str) -> str:
    """A CDX response body: JSON-LINES, one object per line, no array."""
    return "\n".join(json.dumps({"url": u}) for u in urls)


# --- token extraction: the junk the live index actually contains -----------


@pytest.mark.parametrize(
    "url,expected",
    [
        # The plain case.
        ("https://job-boards.greenhouse.io/acme", "acme"),
        # Deeper path — the posting id is not part of the board name.
        ("https://boards.greenhouse.io/acme/jobs/4644852002", "acme"),
        # Query string. Measured on the live index: utm_*, gh_jid, ref.
        (
            "https://boards.greenhouse.io/acme/jobs/636?utm_source=aworker.io&ref=x",
            "acme",
        ),
        # Trailing slash.
        ("https://job-boards.greenhouse.io/acme/", "acme"),
        # Case is normalised, exactly as the GitHub miner normalises it.
        ("https://job-boards.greenhouse.io/AcmeCorp/jobs/1", "acmecorp"),
        # The embed form names the board in the query string and puts vendor
        # plumbing in the path, so the path must not win.
        (
            "https://boards.greenhouse.io/embed/job_board?for=acme",
            "acme",
        ),
        # Vendor plumbing on its own is not a board.
        ("https://boards.greenhouse.io/embed/job_board", None),
        # robots.txt at the host root: rejected by the token shape (a dot
        # cannot appear in a token), not by luck.
        ("https://job-boards.greenhouse.io/robots.txt", None),
        # The host root names nothing.
        ("https://job-boards.greenhouse.io/", None),
    ],
)
def test_greenhouse_tokens_survive_the_junk(url, expected):
    assert token_from_url(GREENHOUSE_NEW, url) == expected


@pytest.mark.parametrize(
    "target,url,expected",
    [
        (ASHBY, "https://jobs.ashbyhq.com/openai", "openai"),
        (ASHBY, "https://jobs.ashbyhq.com/ramp/some-uuid?utm_source=x", "ramp"),
        (
            SMARTRECRUITERS,
            "https://jobs.smartrecruiters.com/Continental/744000116156242",
            "continental",
        ),
        (WORKABLE, "https://apply.workable.com/cloudacademy/", "cloudacademy"),
        (
            WORKABLE,
            "https://apply.workable.com/consigli-construction-1/j/ABC123/",
            "consigli-construction-1",
        ),
        # 1,150 of these on Workable alone in one crawl. `/j/<code>` is a
        # shortlink; `j` is vendor plumbing and never a board.
        (WORKABLE, "https://apply.workable.com/j/DEADBEEF01", None),
        # Recruitee is a subdomain vendor: the token is the leftmost label.
        (RECRUITEE, "https://enpicom.recruitee.com/o/some-role", "enpicom"),
        (RECRUITEE, "https://monizze.recruitee.com/", "monizze"),
        # A deeper host is not a board called "www.acme".
        (RECRUITEE, "https://www.acme.recruitee.com/", None),
        # The bare apex has no label at all.
        (RECRUITEE, "https://recruitee.com/careers_not_hosted", None),
    ],
)
def test_per_vendor_token_shapes(target, url, expected):
    assert token_from_url(target, url) == expected


def test_token_convention_matches_the_github_miner_exactly():
    """The same careers URL must yield the same token from both sources.

    This is the property the (ats, token) dedupe key rests on. If the two
    modules disagreed by so much as a case fold, every Greenhouse board found
    by both would be stored twice and probed forever.
    """
    for raw in (
        "https://boards.greenhouse.io/AcmeCorp/jobs/123",
        "https://boards.greenhouse.io/acme-corp",
        "https://boards.greenhouse.io/embed/job_board?for=Acme_Corp",
    ):
        mined = gh.extract_boards(raw)
        harvested = token_from_url(GREENHOUSE_OLD, raw)
        assert mined, f"the github miner found nothing in {raw}"
        assert mined[0][1] == harvested


def test_plumbing_segments_come_from_the_github_miners_own_set():
    """Reused, not restated — one list of what is not a company."""
    assert cc._NOT_A_TOKEN is gh._NOT_A_TOKEN
    for plumbing in ("api", "www", "search", "static", "jobs"):
        assert token_from_url(ASHBY, f"https://jobs.ashbyhq.com/{plumbing}") is None


def test_tokens_from_urls_dedupes_and_keeps_first_seen_order():
    urls = [
        "https://jobs.ashbyhq.com/crusoe/a",
        "https://jobs.ashbyhq.com/openai/b",
        "https://jobs.ashbyhq.com/crusoe/c",
        "https://jobs.ashbyhq.com/robots.txt",
    ]
    assert tokens_from_urls(ASHBY, urls) == ["crusoe", "openai"]


# --- the Lever tripwire ----------------------------------------------------


def test_lever_is_queried_rather_than_deleted():
    """Lever returns zero because CCBot is disallowed, and that must be VISIBLE.

    jobs.lever.co/robots.txt carries `User-agent: CCBot / Disallow: /`, so the
    index holds Lever's robots.txt and no board URL. The query stays so a run
    reports the block, and so the first non-zero count is the day Lever
    unblocks CCBot. A deleted query would make somebody re-derive that in a
    year from an unexplained gap.
    """
    assert LEVER.ats == "lever"
    assert LEVER.url.startswith("jobs.lever.co")
    assert LEVER.expect_empty, "the tripwire must carry the reason it is empty"
    assert "ccbot" in LEVER.expect_empty.lower()

    # What the index actually holds for that prefix: robots.txt, nothing else.
    assert tokens_from_urls(LEVER, ["https://jobs.lever.co/robots.txt"]) == []


def test_workday_is_not_harvestable_and_is_absent():
    """Not an oversight. The tenant/host/site triple is not in the URL."""
    assert "workday" not in {t.ats for t in TARGETS}
    assert not any("myworkdayjobs" in t.url for t in TARGETS)


# --- the India hint is a hint ----------------------------------------------


@pytest.mark.parametrize(
    "token",
    ["alphasenseindia", "bluevineindia", "kraftonindia", "bharat-payments"],
)
def test_india_hint_fires_on_an_anchored_marker(token):
    assert india_hint(token) is not None


@pytest.mark.parametrize(
    "token",
    # Every one of these was flagged by an unanchored substring search against
    # the live harvest — on the "desi" inside "design". Measured, not imagined.
    ["codesignal", "cannondesign", "andesite", "avex-designs", "desilva-gates"],
)
def test_india_hint_does_not_fire_on_design(token):
    assert india_hint(token) is None


def test_india_hint_misses_real_india_boards_and_that_is_expected():
    """Documented weakness, asserted so nobody later mistakes it for a filter.

    Both of these are real Indian boards present in the live harvest. Neither
    puts a country in its token, so neither is flagged. Measured on a full
    CC-MAIN-2026-30 harvest the hint fires on 11 of 11,474 tokens (0.10%), and
    one of the eleven is an American school. Real verification is the two-bar
    test, run later.
    """
    assert india_hint("phonepe") is None
    assert india_hint("meesho") is None


def test_the_hint_never_gates_storage():
    """An unflagged token produces a row exactly like a flagged one."""
    flagged = harvest_row(GREENHOUSE_NEW, "bluevineindia", "CC-MAIN-2026-30")
    plain = harvest_row(GREENHOUSE_NEW, "phonepe", "CC-MAIN-2026-30")

    assert len(flagged) == len(plain)
    assert json.loads(flagged[3])["india_hint"] is True
    assert "india_hint" not in json.loads(plain[3])
    # Same statement, same arity, same table. The hint is cargo, not a gate.
    assert flagged[1] == plain[1] == "greenhouse"


def test_the_stored_hint_carries_its_own_caveat():
    config = json.loads(harvest_row(ASHBY, "bluevineindia", "CC-MAIN-2026-30")[3])
    assert "guess" in config["india_hint_reason"].lower()


# --- the row ---------------------------------------------------------------


def test_harvest_row_records_provenance():
    row = harvest_row(GREENHOUSE_OLD, "acme", "CC-MAIN-2026-30")
    name, ats, token, config, services = row
    assert (name, ats, token) == ("acme", "greenhouse", "acme")
    parsed = json.loads(config)
    assert parsed["harvest"] == "commoncrawl"
    assert parsed["crawl_id"] == "CC-MAIN-2026-30"
    assert parsed["host"] == "boards.greenhouse.io"
    assert services is False


def test_services_flag_reuses_the_discovery_heuristic():
    from crawler.discover import looks_like_services

    assert harvest_row(ASHBY, "acme-consulting", "c")[4] is looks_like_services(
        "acme-consulting"
    )


# --- the seal: this module writes one table --------------------------------


def test_the_statement_names_only_discovered_companies():
    """Read the SQL, not the intent.

    A harvest that touched `companies` would promote unverified boards into the
    hourly crawl; one that touched `jobs` would break the silent-seed detection
    that keeps a newly-tracked company out of everybody's inbox. Neither table
    may be named at all.
    """
    sql = " ".join(HARVEST_SQL.lower().split())

    assert " jobs " not in sql
    assert "into jobs" not in sql
    # `discovered_companies` legitimately contains the substring "companies",
    # so remove it before asking whether the real table is named.
    assert "companies" not in sql.replace("discovered_companies", "")
    assert "insert into discovered_companies" in sql
    assert "on conflict (ats, token) do nothing" in sql
    # DO UPDATE would let a harvest overwrite a row the discovery sweep filled
    # in from a real board fetch.
    assert "do update" not in sql


def test_the_harvester_imports_no_board_client():
    """It cannot crawl a board even by accident: it never imports one."""
    import scripts.harvest_commoncrawl as harvester

    source = (
        __import__("pathlib").Path(harvester.__file__).read_text(encoding="utf-8")
    )
    for forbidden in ("fetchers.tier1", "fetchers.workday", "fetchers.tier3", "probe_boards"):
        assert forbidden not in source


def test_the_row_leaves_job_count_and_city_hits_unset():
    """NULL means 'never probed', which is true and is not the same as 0."""
    sql = " ".join(HARVEST_SQL.lower().split())
    assert "job_count" not in sql
    assert "city_hits" not in sql
    assert "last_posting_at" not in sql
    assert "promoted" not in sql


# --- the seal, again, by counting rows -------------------------------------


async def test_a_harvest_writes_only_discovered_companies(conn):
    await conn.execute(
        "INSERT INTO companies (name, ats, token, config, category, tracked, india_scoped) "
        "VALUES ($1, $2, $3, '{}', 'unknown', TRUE, FALSE)",
        "Existing",
        "greenhouse",
        "existing",
    )
    before_companies = await conn.fetchval("SELECT count(*) FROM companies")
    before_jobs = await conn.fetchval("SELECT count(*) FROM jobs")

    rows = [
        harvest_row(GREENHOUSE_NEW, "acme", "CC-MAIN-2026-30"),
        harvest_row(ASHBY, "openai", "CC-MAIN-2026-30"),
    ]
    await conn.executemany(HARVEST_SQL, rows)

    assert await conn.fetchval("SELECT count(*) FROM discovered_companies") == 2
    assert await conn.fetchval("SELECT count(*) FROM companies") == before_companies
    assert await conn.fetchval("SELECT count(*) FROM jobs") == before_jobs


async def test_source_marks_the_rows_as_commoncrawl_harvested(conn):
    await conn.executemany(
        HARVEST_SQL, [harvest_row(ASHBY, "openai", "CC-MAIN-2026-30")]
    )
    row = (await conn.fetch("SELECT * FROM discovered_companies"))[0]
    assert row["source"] == "commoncrawl"
    assert row["job_count"] is None
    assert row["city_hits"] is None
    assert row["promoted"] == 0  # the column default; nothing was promoted


async def test_re_running_a_harvest_changes_nothing(conn):
    """ON CONFLICT DO NOTHING, proven against the real UNIQUE (ats, token)."""
    row = harvest_row(ASHBY, "openai", "CC-MAIN-2026-30")
    await conn.executemany(HARVEST_SQL, [row])
    await conn.executemany(HARVEST_SQL, [row])
    assert await conn.fetchval("SELECT count(*) FROM discovered_companies") == 1


async def test_a_harvest_never_overwrites_what_the_sweep_learned(conn):
    """The reason this statement is DO NOTHING rather than DO UPDATE.

    A row the discovery sweep wrote knows things a harvest does not: it fetched
    the board, counted the jobs, labelled the cities, and may have been
    promoted. Re-harvesting that same token must not flatten any of it.
    """
    from crawler.discover import LOG_SQL

    await conn.execute(
        LOG_SQL,
        "OpenAI",
        "ashby",
        "openai",
        json.dumps({"candidate": "OpenAI"}),
        42,
        None,
        ["blr"],
        False,
        "github",
        True,
    )

    await conn.executemany(
        HARVEST_SQL, [harvest_row(ASHBY, "openai", "CC-MAIN-2026-30")]
    )

    row = (await conn.fetch("SELECT * FROM discovered_companies"))[0]
    assert row["name"] == "OpenAI"  # not flattened to the bare token
    assert row["job_count"] == 42
    assert row["source"] == "github"
    assert row["promoted"] == 1
    assert json.loads(row["city_hits"]) == ["blr"]


# --- dedupe / resumability -------------------------------------------------


async def test_known_boards_are_skipped_without_a_write(conn):
    """The resume model: what is already known costs nothing to see again."""
    from crawler.discover import load_known

    await conn.execute(
        "INSERT INTO companies (name, ats, token, config, category, tracked, india_scoped) "
        "VALUES ($1, $2, $3, '{}', 'unknown', TRUE, FALSE)",
        "Acme",
        "greenhouse",
        "acme",
    )
    await conn.executemany(
        HARVEST_SQL, [harvest_row(ASHBY, "openai", "CC-MAIN-2026-30")]
    )

    known = await load_known(conn)
    known_boards = known.company_boards | known.logged_boards

    assert ("greenhouse", "acme") in known_boards  # from companies
    assert ("ashby", "openai") in known_boards  # from a previous harvest
    assert ("ashby", "crusoe") not in known_boards


# --- the CDX client, stubbed -----------------------------------------------


async def test_cdx_bodies_are_parsed_as_json_lines(monkeypatch):
    """The index answers JSON-LINES. json.loads on the whole body would fail."""
    body = cdx_lines(
        "https://jobs.ashbyhq.com/crusoe/x",
        "https://jobs.ashbyhq.com/openai/y",
    )
    assert json.loads(body.splitlines()[0])["url"].endswith("/crusoe/x")
    with pytest.raises(ValueError):
        json.loads(body)  # the whole body is not a JSON document

    async def fake_get(url, **kwargs):
        assert kwargs["want_json"] is False  # or res.text would be unparsed
        return _FakeFetched(body)

    monkeypatch.setattr(cc, "polite_get", fake_get)
    urls = await cc.fetch_page("CC-MAIN-2026-30", ASHBY, 0)
    assert tokens_from_urls(ASHBY, urls) == ["crusoe", "openai"]


async def test_a_truncated_last_line_does_not_discard_the_page(monkeypatch):
    async def fake_get(url, **kwargs):
        return _FakeFetched(
            cdx_lines("https://jobs.ashbyhq.com/crusoe/x") + '\n{"url": "https://jo'
        )

    monkeypatch.setattr(cc, "polite_get", fake_get)
    urls = await cc.fetch_page("CC-MAIN-2026-30", ASHBY, 0)
    assert tokens_from_urls(ASHBY, urls) == ["crusoe"]


async def test_redirect_captures_are_kept_not_filtered_by_status(monkeypatch):
    """Every boards.greenhouse.io record is a 301 stub pointing at the new host.

    Filtering to status 200 would therefore drop that entire host, and would
    silently discard redirect captures on every other vendor too. The token is
    in the URL whether the capture was a page or a redirect, and this module
    harvests names rather than reading content — so status is not its business.
    """
    seen = {}

    async def fake_get(url, **kwargs):
        seen["params"] = kwargs.get("params", {})
        return _FakeFetched(cdx_lines("https://boards.greenhouse.io/acme/jobs/1"))

    monkeypatch.setattr(cc, "polite_get", fake_get)
    urls = await cc.fetch_page("CC-MAIN-2026-30", GREENHOUSE_OLD, 0)

    assert "filter" not in seen["params"]
    assert tokens_from_urls(GREENHOUSE_OLD, urls) == ["acme"]


async def test_num_pages_reads_the_page_count(monkeypatch):
    async def fake_get(url, **kwargs):
        assert kwargs["params"]["showNumPages"] == "true"
        return _FakeFetched('{"pages": 5, "pageSize": 5, "blocks": 23}')

    monkeypatch.setattr(cc, "polite_get", fake_get)
    assert await cc.num_pages("CC-MAIN-2026-30", GREENHOUSE_NEW) == 5


async def test_an_unindexed_prefix_is_zero_pages_not_a_crash(monkeypatch):
    """A prefix the index never saw answers 404 with text. That is an answer."""

    async def fake_get(url, **kwargs):
        return _FakeFetched("No Captures found", ok=False, status=404)

    monkeypatch.setattr(cc, "polite_get", fake_get)
    assert await cc.num_pages("CC-MAIN-2026-30", LEVER) == 0
    assert await cc.fetch_page("CC-MAIN-2026-30", LEVER, 0) == []


async def test_recruitee_is_queried_by_domain_not_prefix(monkeypatch):
    """A subdomain vendor has nothing for a path prefix to bite on."""
    seen = {}

    async def fake_get(url, **kwargs):
        seen.update(kwargs.get("params", {}))
        return _FakeFetched("")

    monkeypatch.setattr(cc, "polite_get", fake_get)
    await cc.fetch_page("CC-MAIN-2026-30", RECRUITEE, 0)
    assert seen["matchType"] == "domain"
    assert seen["url"] == "recruitee.com"


async def test_only_the_url_field_is_requested(monkeypatch):
    """fl=url is ~10x less data for the one field this module reads."""
    seen = {}

    async def fake_get(url, **kwargs):
        seen.update(kwargs.get("params", {}))
        return _FakeFetched("")

    monkeypatch.setattr(cc, "polite_get", fake_get)
    await cc.fetch_page("CC-MAIN-2026-30", ASHBY, 3)
    assert seen["fl"] == "url"
    assert seen["output"] == "json"
    assert seen["page"] == "3"


async def test_every_request_is_paced(monkeypatch):
    """The live index answers 502 under repeated querying. Pace it."""
    delays = []

    async def fake_get(url, **kwargs):
        delays.append(kwargs.get("min_delay"))
        return _FakeFetched("")

    monkeypatch.setattr(cc, "polite_get", fake_get)
    await cc.fetch_page("CC-MAIN-2026-30", ASHBY, 0)
    await cc.num_pages("CC-MAIN-2026-30", ASHBY)
    await cc.latest_crawl_id()

    assert delays == [cc.CC_MIN_DELAY] * 3
    assert cc.CC_MIN_DELAY >= 5.0


async def test_latest_crawl_id_reads_collinfo(monkeypatch):
    class _Json:
        ok = True
        status = 200
        text = None
        error = None
        json = [
            {"id": "CC-MAIN-2026-30", "name": "July 2026 Index"},
            {"id": "CC-MAIN-2026-25", "name": "June 2026 Index"},
        ]

    async def fake_get(url, **kwargs):
        assert url.endswith("collinfo.json")
        return _Json()

    monkeypatch.setattr(cc, "polite_get", fake_get)
    assert await cc.latest_crawl_id() == "CC-MAIN-2026-30"


async def test_an_unreadable_collinfo_is_none_not_a_crash(monkeypatch):
    async def fake_get(url, **kwargs):
        return _FakeFetched("nope", ok=False, status=503)

    monkeypatch.setattr(cc, "polite_get", fake_get)
    assert await cc.latest_crawl_id() is None


# --- targets ---------------------------------------------------------------


def test_both_greenhouse_hosts_are_queried():
    """The old host is redundant this crawl, and is queried anyway.

    Measured on a full CC-MAIN-2026-30 harvest: boards.greenhouse.io added zero
    tokens job-boards.greenhouse.io had not already produced. One request a run
    keeps it visible in the report rather than assumed away — it is still a live
    redirect origin, and its captures still name their boards.
    """
    hosts = {t.url for t in TARGETS if t.ats == "greenhouse"}
    assert hosts == {"job-boards.greenhouse.io/*", "boards.greenhouse.io/*"}


def test_the_harvestable_set_is_the_five_plus_the_tripwire():
    assert {t.ats for t in TARGETS} == {
        "greenhouse",
        "ashby",
        "smartrecruiters",
        "workable",
        "recruitee",
        "lever",
    }
