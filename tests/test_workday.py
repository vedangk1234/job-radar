"""Workday fetcher: mapping, id/URL construction, facet discovery, failure modes.

No network. Every payload here is either a real saved response (tests/data/
workday_board.json, workday_facets.json — trimmed but not invented) or a
hand-built minimal one for a failure case that a live tenant will not perform
on demand.

The facet tests are the ones worth reading. They encode two things that were
found by checking live tenants and that no amount of documentation would have
told us: the country facet has a different NAME on every tenant, and a
substring match on "india" selects Indiana, USA.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fetchers.normalize import JOB_KEYS, city_label
from fetchers.resolve import parse_workday_url
from fetchers.workday import (
    WorkdayError,
    _check,
    build_url,
    find_india_facet,
    map_posting,
    parse_native_id,
)

DATA_DIR = Path(__file__).resolve().parent / "data"

NVIDIA = ("nvidia.wd5.myworkdayjobs.com", "NVIDIAExternalCareerSite", "en-US")


def load(name: str):
    return json.loads((DATA_DIR / name).read_text(encoding="utf-8"))


@pytest.fixture
def board():
    return load("workday_board.json")


@pytest.fixture
def facets():
    return load("workday_facets.json")


class Fetched:
    """The bits of fetchers.http.Fetched that _check reads."""

    def __init__(self, ok=True, json=None, status=200, error=None):
        self.ok, self.json, self.status, self.error = ok, json, status, error


# --- mapping ---------------------------------------------------------------


def test_real_board_maps_without_crashing(board):
    """Every posting on a real India page produces the one canonical shape."""
    rows = [map_posting(p, *NVIDIA) for p in board["jobPostings"]]

    assert len(rows) == 20
    for row in rows:
        assert tuple(row) == JOB_KEYS
        assert row["title"] and row["native_id"] and row["url"]
        assert row["url"].startswith("https://nvidia.wd5.myworkdayjobs.com/en-US/")


def test_city_labels_are_sane_on_a_real_india_board(board):
    """An India-scoped page must not land in `other` — that label drops out."""
    labels = [city_label(map_posting(p, *NVIDIA)["location"]) for p in board["jobPostings"]]

    assert "other" not in labels
    assert set(labels) <= {"blr", "hyd", "pune", "india-unclear"}
    assert labels.count("blr") > 0


def test_count_only_location_recovers_a_city_from_the_path(board):
    """"3 Locations" is a count, not a place. ~26% of NVIDIA's India rows say it.

    Left alone those rows label as `other` and fall out of the funnel, so the
    externalPath's own location segment is folded in. This asserts the recovery,
    not just that nothing crashed.
    """
    counted = [p for p in board["jobPostings"] if "Locations" in (p.get("locationsText") or "")]
    assert counted, "fixture no longer contains an 'N Locations' row"

    for posting in counted:
        row = map_posting(posting, *NVIDIA)
        # The vendor's own string survives verbatim alongside the recovered city.
        assert posting["locationsText"] in row["location"]
        assert city_label(row["location"]) != "other"


def test_posted_at_is_always_none(board):
    """postedOn is "Posted 3 Days Ago" — advisory prose, never a date."""
    assert all(p.get("postedOn") for p in board["jobPostings"])
    assert all(map_posting(p, *NVIDIA)["posted_at"] is None for p in board["jobPostings"])


def test_department_is_never_the_req_id(board):
    """bulletFields holds exactly the req id, which is already native_id."""
    for posting in board["jobPostings"]:
        row = map_posting(posting, *NVIDIA)
        assert row["department"] != row["native_id"]
        assert row["department"] is None or not row["department"].startswith("JR")


def test_department_uses_job_family_when_the_tenant_sends_one():
    posting = {
        "title": "Engineer",
        "externalPath": "/job/India-Pune/Engineer_JR1",
        "locationsText": "India, Pune",
        "jobFamily": "Software Engineering",
        "bulletFields": ["JR1"],
    }
    assert map_posting(posting, *NVIDIA)["department"] == "Software Engineering"


# --- native_id -------------------------------------------------------------


@pytest.mark.parametrize(
    "path, expected",
    [
        # Real externalPath values from live NVIDIA / Adobe / Salesforce boards.
        ("/job/India-Bengaluru/Software-Configuration-Management-Engineer---Hardware_JR2019717", "JR2019717"),
        ("/job/India-Pune/Senior-System-Software-Engineer---Local-AI-Automation_JR2021900", "JR2021900"),
        ("/job/Bangalore/Senior-Computer-Scientist_R170168-1", "R170168-1"),
        ("/job/India---Hyderabad/Accountant_JR349380-1", "JR349380-1"),
        ("/job/Remote-India/Solutions-Consultant_R169899", "R169899"),
    ],
)
def test_native_id_from_real_external_paths(path, expected):
    """The trailing token, INCLUDING Workday's -N disambiguator.

    The -N is kept rather than stripped because it is what makes the posting's
    own URL unique: two live reqs can share a bare req id, and collapsing them
    would make one of the two invisible forever.
    """
    assert parse_native_id(path) == expected


def test_native_id_falls_back_to_a_hash_not_none():
    """No req id -> a stable hash of the path.

    None would send canonical_id to its title+location hash, and a Workday board
    routinely carries several distinct reqs with the same title and city.
    """
    first = parse_native_id("/job/India-Pune/No-Req-Id-Here")
    assert first.startswith("wd-")
    assert first == parse_native_id("/job/India-Pune/No-Req-Id-Here")
    assert first != parse_native_id("/job/India-Pune/A-Different-Posting")


def test_native_id_of_nothing_is_none():
    assert parse_native_id("") is None
    assert parse_native_id(None) is None


# --- url -------------------------------------------------------------------


def test_url_matches_the_format_verified_in_a_browser():
    """This exact string was opened and returned 200 with the posting rendered."""
    assert build_url(
        "nvidia.wd5.myworkdayjobs.com",
        "NVIDIAExternalCareerSite",
        "/job/India-Bengaluru/Software-Configuration-Management-Engineer---Hardware_JR2019717",
        "en-US",
    ) == (
        "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite"
        "/job/India-Bengaluru/Software-Configuration-Management-Engineer---Hardware_JR2019717"
    )


def test_url_without_a_lang_omits_the_segment():
    """Both forms answer 200 live; the lang-less one must not emit a bare //."""
    url = build_url("adobe.wd5.myworkdayjobs.com", "external_experienced", "/job/Bangalore/X_R1", None)
    assert url == "https://adobe.wd5.myworkdayjobs.com/external_experienced/job/Bangalore/X_R1"


def test_url_is_none_when_a_piece_is_missing():
    assert build_url("", "site", "/job/x", "en-US") is None
    assert build_url("host", "site", None, "en-US") is None


# --- facet discovery -------------------------------------------------------


def test_finds_the_country_facet_on_every_real_tenant_shape(facets):
    """Three tenants, three different names for the same idea.

    NVIDIA has no country facet at all and carries India under a location
    hierarchy; Adobe uses `locationCountry`; Salesforce uses a custom field
    name. Discovery is by VALUE, which is why one rule covers all three.
    """
    assert find_india_facet(facets["nvidia"]) == (
        "locationHierarchy1",
        "2fcb99c455831013ea52b82135ba3266",
    )
    assert find_india_facet(facets["adobe"]) == (
        "locationCountry",
        "c4f78be1a8f14da0ab49ce1162348a5e",
    )
    param, wid = find_india_facet(facets["salesforce"])
    assert param.endswith("Country_from_Job_Posting_Location_Extended")
    assert wid == "c4f78be1a8f14da0ab49ce1162348a5e"


def test_indiana_is_never_mistaken_for_india(facets):
    """Salesforce's `locations` facet really does list Indiana, USA.

    A `"india" in descriptor.lower()` check scopes the company to Indianapolis
    and the failure is silent — the board still returns jobs, just the wrong
    country's. Exact matching is the whole defence, so it gets its own test.
    """
    locations = [
        sub
        for group in facets["salesforce"]["facets"]
        for sub in (group.get("values") or [])
        if isinstance(sub, dict) and sub.get("facetParameter") == "locations"
    ][0]
    indiana = [v for v in locations["values"] if v["descriptor"].startswith("Indiana")]
    assert indiana, "fixture no longer contains the Indiana values this guards against"

    param, wid = find_india_facet(facets["salesforce"])
    assert param != "locations"
    assert wid not in {v["id"] for v in indiana}


def test_no_india_facet_returns_none():
    payload = {
        "facets": [
            {
                "facetParameter": "locationCountry",
                "descriptor": "Country",
                "values": [{"descriptor": "Germany", "id": "de1"}, {"descriptor": "Indiana", "id": "in1"}],
            }
        ]
    }
    assert find_india_facet(payload) is None


def test_a_country_facet_outranks_a_site_facet():
    """Both name India; the country one is the one that means "in India"."""
    payload = {
        "facets": [
            {
                "facetParameter": "locations",
                "descriptor": "Sites",
                "values": [{"descriptor": "India", "id": "site-wid"}],
            },
            {
                "facetParameter": "locationCountry",
                "descriptor": "Country",
                "values": [{"descriptor": "India", "id": "country-wid"}],
            },
        ]
    }
    assert find_india_facet(payload) == ("locationCountry", "country-wid")


# --- failure semantics -----------------------------------------------------


def test_empty_board_is_not_an_error():
    """total=0 on a well-formed response is a real, empty board.

    This is the Workable lesson: if an empty answer and a broken answer look the
    same to the crawl loop, three bad days close every job on the board and the
    recovery day re-alerts all of them as new.
    """
    payload = _check(Fetched(json={"total": 0, "jobPostings": [], "facets": []}), "u", 0)
    assert payload["total"] == 0
    assert payload["jobPostings"] == []


def test_http_404_raises():
    with pytest.raises(WorkdayError):
        _check(Fetched(ok=False, status=404, error="HTTP 404"), "u", 0)


def test_html_error_page_raises():
    with pytest.raises(WorkdayError):
        _check(Fetched(ok=False, status=200, error="response was not JSON"), "u", 0)


def test_workday_error_envelope_raises():
    """A 200 that parses as JSON but is an error envelope, not a board."""
    with pytest.raises(WorkdayError):
        _check(Fetched(json={"errorCode": "S21", "httpStatus": 404}), "u", 0)


def test_non_object_response_raises():
    with pytest.raises(WorkdayError):
        _check(Fetched(json=[{"title": "x"}]), "u", 0)


def test_malformed_job_postings_raises():
    with pytest.raises(WorkdayError):
        _check(Fetched(json={"total": 5, "jobPostings": "not a list"}), "u", 0)


# --- resolve ---------------------------------------------------------------


@pytest.mark.parametrize(
    "url, host, tenant, site, lang",
    [
        (
            "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite",
            "nvidia.wd5.myworkdayjobs.com", "nvidia", "NVIDIAExternalCareerSite", "en-US",
        ),
        (
            "https://adobe.wd5.myworkdayjobs.com/external_experienced",
            "adobe.wd5.myworkdayjobs.com", "adobe", "external_experienced", "en-US",
        ),
        (
            "https://salesforce.wd12.myworkdayjobs.com/en-US/External_Career_Site",
            "salesforce.wd12.myworkdayjobs.com", "salesforce", "External_Career_Site", "en-US",
        ),
        (
            "https://acme.wd3.myworkdayjobs.com/de-DE/Careers",
            "acme.wd3.myworkdayjobs.com", "acme", "Careers", "de-DE",
        ),
    ],
)
def test_resolve_parses_each_url_pattern(url, host, tenant, site, lang):
    config = parse_workday_url(url)
    assert (config["host"], config["tenant"], config["site"], config["lang"]) == (
        host, tenant, site, lang
    )
    assert "ambiguous" not in config


def test_resolve_strips_a_posting_off_a_pasted_url():
    """Pasting the job you are looking at should still yield the board."""
    config = parse_workday_url(
        "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite"
        "/job/India-Bengaluru/ASIC-Design-and-STA-Engineer_JR2021789-1"
    )
    assert config["site"] == "NVIDIAExternalCareerSite"
    assert config["tenant"] == "nvidia"


def test_resolve_reads_an_api_url():
    config = parse_workday_url("https://acme.wd3.myworkdayjobs.com/wday/cxs/acmecorp/TheSite/jobs")
    assert (config["tenant"], config["site"]) == ("acmecorp", "TheSite")


def test_resolve_flags_a_vanity_host_instead_of_guessing_silently():
    """The tenant is simply not in a custom domain. Say so rather than invent it."""
    config = parse_workday_url("https://careers.acme.com/en-US/AcmeCareers")
    assert config["site"] == "AcmeCareers"
    assert config["host"] == "careers.acme.com"
    assert "tenant" in config["ambiguous"]


def test_resolve_accepts_a_url_with_no_scheme():
    config = parse_workday_url("nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite")
    assert config["host"] == "nvidia.wd5.myworkdayjobs.com"
    assert config["tenant"] == "nvidia"


# --- pagination, offline ---------------------------------------------------


def fake_workday(pages, total, facets=None):
    """A polite_post stand-in serving `pages` and wrapping past `total`.

    The wrap is the point: a live tenant asked for offset=10000 of 212 answers
    200 with page 1 again, so this fake does too. A paginator that stops on an
    empty page never stops against that.
    """

    async def post(url, *, json_body=None, headers=None, want_json=True, min_delay=None):
        offset = json_body["offset"]
        index = (offset // 20) if offset < total else 0  # <- the wrap
        page = pages[index] if index < len(pages) else pages[0]
        return Fetched(json={"total": total, "jobPostings": page, "facets": facets or []})

    return post


def posting(n, city="India, Pune"):
    return {
        "title": f"Engineer {n}",
        "externalPath": f"/job/India-Pune/Engineer-{n}_JR{n}",
        "locationsText": city,
        "postedOn": "Posted Today",
        "bulletFields": [f"JR{n}"],
    }


COUNTRY_FACET = [
    {
        "facetParameter": "locationCountry",
        "descriptor": "Country",
        "values": [{"descriptor": "India", "id": "india-wid", "count": 30}],
    }
]


async def test_pagination_collects_every_page_and_stops_on_total(monkeypatch):
    from fetchers import workday

    pages = [[posting(i) for i in range(p * 20, p * 20 + 20)] for p in range(3)]
    monkeypatch.setattr(workday, "polite_post", fake_workday(pages, 60, COUNTRY_FACET))

    rows = await workday.fetch_workday(
        "acme", {"host": "acme.wd1.myworkdayjobs.com", "site": "Careers"}, save_raw=False
    )

    assert len(rows) == 60
    assert len({r["native_id"] for r in rows}) == 60


async def test_pagination_does_not_loop_forever_when_total_is_overstated(monkeypatch):
    """`total` says 500, the tenant only has 20 and wraps. Stop anyway.

    Without the "a page that adds nothing new is a wrap" stop, this test hangs
    rather than fails — which is exactly what it would do in production.
    """
    from fetchers import workday

    monkeypatch.setattr(
        workday, "polite_post", fake_workday([[posting(i) for i in range(20)]], 500, COUNTRY_FACET)
    )

    rows = await workday.fetch_workday(
        "acme", {"host": "acme.wd1.myworkdayjobs.com", "site": "Careers"}, save_raw=False
    )

    assert len(rows) == 20


async def test_empty_board_returns_no_rows_rather_than_raising(monkeypatch):
    from fetchers import workday

    monkeypatch.setattr(workday, "polite_post", fake_workday([[]], 0, COUNTRY_FACET))

    assert await workday.fetch_workday(
        "acme", {"host": "acme.wd1.myworkdayjobs.com", "site": "Careers"}, save_raw=False
    ) == []


async def test_missing_host_or_site_raises_rather_than_building_a_junk_url():
    from fetchers import workday

    with pytest.raises(WorkdayError):
        await workday.fetch_workday("acme", {"site": "Careers"}, save_raw=False)


# --- crawl-loop registration -----------------------------------------------


def test_workday_is_registered_in_the_crawl_loop():
    """Step 3's loop dispatches on ats with no vendor special-casing.

    A company row with ats='workday' has to flow through seed detection, the
    three guards and the diff untouched, which it only does if the key is here.
    """
    from crawler.loop import FETCHERS

    assert "workday" in FETCHERS


async def test_a_workday_failure_trips_guard_1(conn):
    """A dead tenant must leave the jobs table exactly as it was.

    fetch_workday raising (rather than returning []) is what buys this. If it
    returned [], the diff would put every open job in `missing` instead.
    """
    from crawler.loop import Company, crawl

    await conn.add_company(7, "Acme")
    await conn.add_job("workday:7:JR1", company_id=7)
    before = await conn.snapshot()

    async def dead(token, config):
        raise WorkdayError("HTTP 404")

    company = Company(id=7, name="Acme", ats="workday", token="acme", config={})
    result = await crawl(conn, company, seeding=False, fetchers={"workday": dead})

    assert result.guard_tripped
    assert result.guard.startswith("fetch_error:")
    assert await conn.snapshot() == before
