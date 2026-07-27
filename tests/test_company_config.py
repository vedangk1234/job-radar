"""Company.from_row must hand back a dict, whatever the row actually holds.

This is a regression suite for one production incident. The dashboard's
add-by-URL route bound `config` as `${JSON.stringify(config)}::jsonb`; postgres.js
resolves that parameter to jsonb and serialises jsonb with its own
JSON.stringify, so the value was encoded twice and the column ended up holding a
jsonb *string* containing JSON text rather than a jsonb object.

asyncpg returns jsonb as text, so the old from_row decoded it exactly once and
got a `str` back. `config or {}` let that non-empty string through, and the first
`config.get("host")` inside the Workday fetcher raised
`'str' object has no attribute 'get'`. Guard 1 caught the exception, declined to
write, and logged it as an unreachable board — so LSEG crawled to zero jobs
every 30 minutes while the report blamed the network rather than the row.

The write side is fixed (dashboard/src/lib/companies.ts) and the row is repaired
(scripts/repair_config.py), which makes these the third line of defence: whatever
turns up in that column, the crawler is handed a mapping and the failure is a
warning rather than an AttributeError from four call frames away.
"""

from __future__ import annotations

import json

import pytest

from crawler.loop import Company, crawl

WORKDAY_CONFIG = {
    "host": "lseg.wd3.myworkdayjobs.com",
    "site": "Careers",
    "lang": "en-US",
    "careers_url": "https://lseg.wd3.myworkdayjobs.com/en-US/Careers",
}


def row(config):
    """A companies row in the shape scripts/crawl.py selects."""
    return {"id": 22, "name": "LSEG", "ats": "workday", "token": "lseg", "config": config}


# --- the shapes that reach from_row ---------------------------------------


def test_jsonb_arriving_as_text_is_decoded_once():
    """The ordinary case: asyncpg hands jsonb back as a JSON string."""
    company = Company.from_row(row(json.dumps(WORKDAY_CONFIG)))
    assert isinstance(company.config, dict)
    assert company.config == WORKDAY_CONFIG
    assert company.config.get("host") == "lseg.wd3.myworkdayjobs.com"


def test_config_already_a_dict_is_passed_through():
    """A driver configured with a jsonb codec, and every test in the suite."""
    company = Company.from_row(row(dict(WORKDAY_CONFIG)))
    assert company.config == WORKDAY_CONFIG


def test_double_encoded_config_is_unpicked_not_returned_as_a_string(capsys):
    """THE BUG. A jsonb string containing JSON text still yields a usable dict.

    Before the fix this returned a `str`, and the AttributeError it caused was
    three frames away inside the fetcher.
    """
    double = json.dumps(json.dumps(WORKDAY_CONFIG))
    company = Company.from_row(row(double))

    assert isinstance(company.config, dict), "double-encoded config leaked through as a str"
    assert company.config == WORKDAY_CONFIG
    assert company.config.get("host") == "lseg.wd3.myworkdayjobs.com"
    # Recovered, but not silently: the row is still wrong and says so.
    assert "2x-encoded" in capsys.readouterr().err


def test_double_encoded_empty_config_survives_too():
    """`'"{}"'::jsonb` — what the by-name path stored. Decodes to a plain {}."""
    company = Company.from_row(row(json.dumps(json.dumps({}))))
    assert company.config == {}


@pytest.mark.parametrize(
    "stored",
    [
        "{}",           # an empty config, the tier-1 case
        '{"url": "https://example.test/careers"}',  # custom_* companies
    ],
)
def test_ordinary_configs_decode_without_complaint(stored, capsys):
    """The common shapes must not print a warning — a noisy guard gets ignored."""
    company = Company.from_row(row(stored))
    assert isinstance(company.config, dict)
    assert capsys.readouterr().err == ""


# --- the shapes that should not exist, and must not crash -----------------


@pytest.mark.parametrize("junk", [None, "", "not json at all", "[1, 2, 3]", "42", '"a string"'])
def test_unusable_config_becomes_an_empty_dict_loudly(junk, capsys):
    """Never a str, never a list, never None — and never silent.

    `{}` is the right fallback rather than a raise: a Workday row with no
    host/site then fails inside the fetcher, which is Guard 1's job, and Guard 1
    writes nothing. A raise here would instead take down scripts/crawl.py before
    any other company had been crawled.
    """
    company = Company.from_row(row(junk))
    assert isinstance(company.config, dict)
    assert company.config == {}
    assert "not an object" in capsys.readouterr().err


def test_decoding_is_bounded():
    """A pathologically nested value terminates instead of spinning."""
    nested = WORKDAY_CONFIG
    for _ in range(12):
        nested = json.dumps(nested)
    company = Company.from_row(row(nested))
    assert isinstance(company.config, dict)


# --- and the end-to-end consequence ---------------------------------------


async def test_a_repaired_company_reaches_the_fetcher_with_a_mapping(conn):
    """The fetcher is handed something it can call .get() on, so no Guard 1 trip.

    This is the assertion that ties the unit behaviour to the incident: the old
    code path ended in `fetch_error:AttributeError` and zero rows written.
    """
    seen: dict = {}

    async def fake_workday(token: str, config: dict) -> list[dict]:
        # Exactly what fetchers/workday.py does first, and what used to raise.
        seen["host"] = config.get("host")
        seen["site"] = config.get("site")
        return []

    company = Company.from_row(row(json.dumps(json.dumps(WORKDAY_CONFIG))))
    result = await crawl(conn, company, seeding=True, fetchers={"workday": fake_workday})

    assert not result.guard_tripped, f"guard tripped: {result.guard}"
    assert seen == {"host": "lseg.wd3.myworkdayjobs.com", "site": "Careers"}
