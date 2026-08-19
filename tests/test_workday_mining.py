"""Mining Workday careers URLs out of GitHub READMEs.

Three properties are worth a test and the rest is reporting:

  1. A URL is only stored when it yields all three unguessable strings. A
     Workday row missing its site is not a company that might work later — it is
     a fetch that fails every 30 minutes forever, which is exactly why
     scripts/add_workday.py refuses an ambiguous parse too.
  2. The parser is the SHARED one. If this module grew its own Workday parser,
     the two would drift and only one of them is the parser the crawler obeys.
  3. `discovered_companies` is the only table named. Not companies, not jobs —
     asserted against the statement TEXT, the same way tests/test_commoncrawl.py
     seals the harvester.

Everything database-facing runs against the SQLite rig in conftest, which
mirrors both tables including UNIQUE (ats, token), so ON CONFLICT has something
real to conflict against.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fetchers import discovery as gh
from fetchers.resolve import parse_workday_url
from scripts.mine_workday_github import (
    CONFIG_KEYS,
    MINE_SQL,
    SOURCE,
    config_from,
    mine_row,
)

GENPACT = "https://genpact.wd108.myworkdayjobs.com/en-US/External_Careers"


# --- finding the URLs in a README ------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        (GENPACT, [GENPACT]),
        (f"| Genpact | [Careers]({GENPACT}) |", [GENPACT]),
        (f'<a href="{GENPACT}">apply</a>', [GENPACT]),
        (f"Apply at {GENPACT}.", [GENPACT]),
        (f"{GENPACT} and again {GENPACT}", [GENPACT]),
    ],
)
def test_a_careers_url_survives_the_markdown_around_it(text, expected):
    """READMEs write these as table cells, links and bare text alike."""
    assert gh.extract_workday_urls(text) == expected


def test_the_site_name_keeps_its_capitals():
    """A Workday site name is case-sensitive; lowercasing it 404s the board."""
    url = "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite"
    assert gh.extract_workday_urls(url) == [url]


def test_a_bare_host_is_still_reported_then_skipped_later():
    """Found is not the same as usable. The report distinguishes the two."""
    found = gh.extract_workday_urls("https://foo.wd1.myworkdayjobs.com")
    assert found == ["https://foo.wd1.myworkdayjobs.com"]
    assert parse_workday_url(found[0])["ambiguous"] == ["site"]


@pytest.mark.parametrize("text", ["", None, "no urls here at all", "greenhouse.io/acme"])
def test_url_extraction_tolerates_junk(text):
    assert gh.extract_workday_urls(text) == []


def test_the_tenant_extractor_still_works_alongside_it():
    """The old name-only path is not replaced — both readings are wanted."""
    assert gh.extract_workday_tenants(GENPACT) == ["genpact"]
    assert gh.extract_boards(GENPACT) == []


# --- all three pieces, or nothing ------------------------------------------


def test_a_full_url_yields_tenant_pod_and_site():
    parsed = parse_workday_url(GENPACT)
    assert parsed["tenant"] == "genpact"
    assert parsed["host"] == "genpact.wd108.myworkdayjobs.com"
    assert parsed["site"] == "External_Careers"
    assert "ambiguous" not in parsed


@pytest.mark.parametrize(
    "url",
    [
        "https://foo.wd1.myworkdayjobs.com",  # no site
        "https://careers.acme.com/en-US/Ext",  # vanity host: tenant is a guess
    ],
)
def test_a_partial_url_parses_ambiguously_and_must_not_be_stored(url):
    """The skip rule, stated as the parser states it."""
    assert parse_workday_url(url).get("ambiguous")


def test_the_miner_reuses_the_shared_parser_rather_than_its_own():
    """Two Workday parsers would drift, and the crawler obeys only one.

    Checked as "this module does no pattern-matching at all": it imports the
    shared parser and compiles nothing, so there is no second reading of a
    Workday URL that could disagree with scripts/add_workday.py's.
    """
    import scripts.mine_workday_github as miner

    source = Path(miner.__file__).read_text(encoding="utf-8")
    assert "from fetchers.resolve import parse_workday_url" in source
    assert "re.compile" not in source
    assert "import re" not in source
    assert miner.parse_workday_url is parse_workday_url


# --- the row it builds ------------------------------------------------------


def test_the_stored_config_is_the_shape_add_workday_writes():
    config = config_from(parse_workday_url(GENPACT), "owner/repo")
    for key in CONFIG_KEYS:
        assert key in config
    assert config["host"] == "genpact.wd108.myworkdayjobs.com"
    assert config["site"] == "External_Careers"
    assert config["lang"] == "en-US"
    # The tenant lives in the token column, not in config — same as any board.
    assert "tenant" not in config


def test_the_row_records_where_it_came_from():
    config = config_from(parse_workday_url(GENPACT), "owner/joblist")
    assert config["mined_from"] == "github:owner/joblist"


def test_the_token_is_the_tenant():
    name, token, config, services = mine_row("genpact", "genpact", {})
    assert token == "genpact"
    assert name == "genpact"
    assert json.loads(config) == {}
    assert services is False


# --- the seal: this module writes one table --------------------------------


def test_the_statement_names_only_discovered_companies():
    """Read the SQL, not the intent.

    A mining run that touched `companies` would put an unverified Workday config
    into the hourly crawl; one that touched `jobs` would break the silent-seed
    detection that keeps a newly-tracked company out of everybody's inbox.
    Neither table may be named at all.
    """
    sql = " ".join(MINE_SQL.lower().split())

    assert " jobs " not in sql
    assert "into jobs" not in sql
    # `discovered_companies` legitimately contains the substring "companies",
    # so remove it before asking whether the real table is named.
    assert "companies" not in sql.replace("discovered_companies", "")
    assert "insert into discovered_companies" in sql
    assert "on conflict (ats, token) do nothing" in sql
    # DO UPDATE would let a mining run overwrite a Workday config a person
    # verified by hand against the live site.
    assert "do update" not in sql


def test_the_miner_imports_no_board_client():
    """It cannot fetch a board even by accident: it never imports one."""
    import scripts.mine_workday_github as miner

    source = Path(miner.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "fetchers.tier1",
        "fetchers.workday",
        "fetchers.tier3",
        "probe_boards",
        "fetch_workday",
    ):
        assert forbidden not in source


def test_the_row_leaves_job_count_and_city_hits_unset():
    """NULL means 'nobody has fetched this board', which is not the same as 0."""
    sql = " ".join(MINE_SQL.lower().split())
    assert "job_count" not in sql
    assert "city_hits" not in sql
    assert "last_posting_at" not in sql
    assert "promoted" not in sql


def test_the_source_marks_the_rows_as_github_mined_workday():
    sql = " ".join(MINE_SQL.lower().split())
    assert f"'{SOURCE}'" in sql
    assert "'workday'" in sql


# --- the seal, again, by counting rows -------------------------------------


async def test_mining_writes_only_discovered_companies(conn):
    await conn.execute(
        "INSERT INTO companies (name, ats, token, config, category, tracked, india_scoped) "
        "VALUES ($1, $2, $3, '{}', 'unknown', TRUE, FALSE)",
        "Existing",
        "greenhouse",
        "existing",
    )
    before_companies = await conn.fetchval("SELECT count(*) FROM companies")
    before_jobs = await conn.fetchval("SELECT count(*) FROM jobs")

    parsed = parse_workday_url(GENPACT)
    await conn.executemany(
        MINE_SQL, [mine_row("genpact", "genpact", config_from(parsed, "o/r"))]
    )

    assert await conn.fetchval("SELECT count(*) FROM discovered_companies") == 1
    assert await conn.fetchval("SELECT count(*) FROM companies") == before_companies
    assert await conn.fetchval("SELECT count(*) FROM jobs") == before_jobs


async def test_the_stored_row_carries_the_full_workday_config(conn):
    parsed = parse_workday_url(GENPACT)
    await conn.executemany(
        MINE_SQL, [mine_row("genpact", "genpact", config_from(parsed, "o/r"))]
    )
    row = (await conn.fetch("SELECT * FROM discovered_companies"))[0]

    assert row["ats"] == "workday"
    assert row["token"] == "genpact"
    assert row["source"] == SOURCE
    assert row["promoted"] in (False, 0)
    config = json.loads(row["config"]) if isinstance(row["config"], str) else row["config"]
    assert config["site"] == "External_Careers"
    assert config["host"] == "genpact.wd108.myworkdayjobs.com"


async def test_re_running_a_mining_pass_changes_nothing(conn):
    row = mine_row("genpact", "genpact", config_from(parse_workday_url(GENPACT), "o/r"))
    await conn.executemany(MINE_SQL, [row])
    await conn.executemany(MINE_SQL, [row])
    assert await conn.fetchval("SELECT count(*) FROM discovered_companies") == 1


async def test_mining_never_overwrites_a_hand_verified_workday_row(conn):
    """DO NOTHING is the whole point: a verified config outranks a mined one."""
    from crawler.discover import LOG_SQL

    await conn.execute(
        LOG_SQL,
        "Genpact",
        "workday",
        "genpact",
        json.dumps({"host": "genpact.wd108.myworkdayjobs.com", "site": "Verified_Site"}),
        41,
        None,
        ["blr"],
        False,
        "hand",
        True,
    )
    await conn.executemany(
        MINE_SQL, [mine_row("genpact", "genpact", config_from(parse_workday_url(GENPACT), "o/r"))]
    )

    row = (await conn.fetch("SELECT * FROM discovered_companies"))[0]
    config = json.loads(row["config"]) if isinstance(row["config"], str) else row["config"]
    assert config["site"] == "Verified_Site"
    assert row["source"] == "hand"
    assert row["job_count"] == 41
