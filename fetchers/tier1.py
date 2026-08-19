"""Tier 1: the six ATSes that publish a clean, unauthenticated JSON board.

Every function here takes a board token and returns the canonical row shape
from normalize.make_job. Every one of them fetches the WHOLE board: no city
filter, no keyword filter, no date cutoff. Deciding which jobs matter happens
much later, and it cannot happen at all if the crawler dropped them here.

Each mapping below was checked against a live board rather than against vendor
docs, because the two disagree in small, load-bearing ways. Field notes are on
each function.

Raw responses are saved as fixtures by default; pass save_raw=False for probes
(resolve.py) where the caller will re-fetch and save the board it settles on.
"""

from __future__ import annotations

from typing import Any

from .fixtures import save_fixture
from .http import polite_get
from .normalize import join_parts, make_job

TIER1_ATS = ("greenhouse", "lever", "ashby", "smartrecruiters", "workable", "recruitee")


def _rows(payload: Any, *keys: str) -> list[dict]:
    """Pull the job array out of a payload that is either a list or a wrapper."""
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return [r for r in value if isinstance(r, dict)]
    return []


def _first_name(value: Any) -> Any:
    """`[{"name": "Engineering"}]` -> "Engineering". Tolerates str/dict/None."""
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, dict):
        return value.get("name") or value.get("label") or value.get("title")
    return value


async def fetch_greenhouse(token: str, *, save_raw: bool = True) -> list[dict]:
    """GET /v1/boards/{token}/jobs.

    Note: no `?content=true`. That flag inflates every row with the full job
    description — megabytes we never store and do not need.

    Live shape: {"jobs": [...], "meta": {...}}; each row has `id`,
    `absolute_url`, `title`, `location: {"name": ...}`, `first_published` and
    `updated_at`. The lean endpoint carries no `departments` array (the
    content=true one does), so department is usually None here — read
    defensively rather than assuming either way.
    """
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
    res = await polite_get(url)
    if not res.ok:
        return []
    if save_raw:
        save_fixture("greenhouse", token, res.json)

    jobs = []
    for row in _rows(res.json, "jobs"):
        location = row.get("location")
        jobs.append(
            make_job(
                native_id=row.get("id") or row.get("internal_job_id"),
                title=row.get("title"),
                location=location.get("name") if isinstance(location, dict) else location,
                url=row.get("absolute_url"),
                posted_at=row.get("first_published") or row.get("updated_at"),
                department=_first_name(row.get("departments")),
            )
        )
    return jobs


async def fetch_lever(token: str, *, save_raw: bool = True) -> list[dict]:
    """GET /v0/postings/{token}?mode=json.

    Live shape: a bare top-level list. The title is `text`, not `title`.
    Location/department live under `categories`; `categories.allLocations` is
    the multi-city list and is preferred over the single `categories.location`.
    `createdAt` is epoch milliseconds.
    """
    url = f"https://api.lever.co/v0/postings/{token}?mode=json"
    res = await polite_get(url)
    if not res.ok:
        return []
    if save_raw:
        save_fixture("lever", token, res.json)

    jobs = []
    for row in _rows(res.json):
        cats = row.get("categories") if isinstance(row.get("categories"), dict) else {}
        all_locations = cats.get("allLocations")
        location = (
            join_parts(all_locations, sep=" | ")
            if isinstance(all_locations, list) and all_locations
            else cats.get("location")
        )
        jobs.append(
            make_job(
                native_id=row.get("id"),
                title=row.get("text"),
                location=location,
                url=row.get("hostedUrl") or row.get("applyUrl"),
                posted_at=row.get("createdAt"),
                department=cats.get("department") or cats.get("team"),
            )
        )
    return jobs


async def fetch_ashby(token: str, *, save_raw: bool = True) -> list[dict]:
    """GET /posting-api/job-board/{token}.

    Live shape: {"jobs": [...], "apiVersion": ...}. Clean and close to the docs:
    `location` is already a display string, `jobUrl` is the public posting.
    `secondaryLocations` carries extra cities when a req is multi-site, so fold
    it into the raw location string rather than dropping it.
    """
    url = f"https://api.ashbyhq.com/posting-api/job-board/{token}"
    res = await polite_get(url)
    if not res.ok:
        return []
    if save_raw:
        save_fixture("ashby", token, res.json)

    jobs = []
    for row in _rows(res.json, "jobs"):
        extra = row.get("secondaryLocations")
        parts = [row.get("location")]
        if isinstance(extra, list):
            parts.extend(_first_name(e) if isinstance(e, dict) else e for e in extra)
        jobs.append(
            make_job(
                native_id=row.get("id"),
                title=row.get("title"),
                location=join_parts(parts, sep=" | "),
                url=row.get("jobUrl") or row.get("applyUrl"),
                posted_at=row.get("publishedAt") or row.get("updatedAt"),
                department=row.get("department") or row.get("team"),
            )
        )
    return jobs


# The default page cap for fetch_smartrecruiters: 50 pages of 100 = 5,000 jobs.
# It exists so a mis-reported `totalFound` cannot spin forever, and it is what
# every crawl path uses. A caller that only needs to know WHETHER a board has
# India jobs (crawler/verify_india.py) can ask for fewer.
SR_MAX_PAGES = 50


async def fetch_smartrecruiters(
    token: str, *, save_raw: bool = True, max_pages: int = SR_MAX_PAGES
) -> list[dict]:
    """GET /v1/companies/{token}/postings?limit=100, paginated by offset.

    Live shape: {"offset", "limit", "totalFound", "content": [...]}. Two traps:
    the title field is `name`, and `ref` is an API URL, not something a human
    can open — the public posting is jobs.smartrecruiters.com/{company}/{id}.
    Pagination stops on totalFound, on an empty page, or at a hard page cap so
    a mis-reported total cannot spin forever.

    `max_pages` is that cap, and it is the only Tier-1 fetcher that needs one:
    this is the only paginated board, and it has real 5,000-job tenants on it
    (aecom2, measured at 4,939 jobs = 50 requests = 50 seconds in one board).
    The default is unchanged, so every existing caller fetches exactly what it
    always did. A caller that lowers it must treat `len(jobs) == max_pages * 100`
    as "this board was truncated" — the loop breaks early on any short page, so
    an exact multiple of the page size is the signal that the cap was the thing
    that stopped it.
    """
    limit, offset, page, jobs = 100, 0, 0, []

    while page < max_pages:
        url = (
            f"https://api.smartrecruiters.com/v1/companies/{token}/postings"
            f"?limit={limit}&offset={offset}"
        )
        res = await polite_get(url)
        if not res.ok:
            break
        if save_raw:
            save_fixture("smartrecruiters", token, res.json, suffix=f"p{page}")

        rows = _rows(res.json, "content")
        if not rows:
            break

        for row in rows:
            loc = row.get("location") if isinstance(row.get("location"), dict) else {}
            company = row.get("company") if isinstance(row.get("company"), dict) else {}
            identifier = company.get("identifier") or token
            posting_id = row.get("id")
            jobs.append(
                make_job(
                    native_id=posting_id,
                    title=row.get("name"),
                    location=loc.get("fullLocation")
                    or join_parts([loc.get("city"), loc.get("region"), loc.get("country")]),
                    url=f"https://jobs.smartrecruiters.com/{identifier}/{posting_id}"
                    if posting_id
                    else row.get("ref"),
                    posted_at=row.get("releasedDate") or row.get("createdOn"),
                    department=_first_name(row.get("department")),
                )
            )

        total = res.json.get("totalFound") if isinstance(res.json, dict) else None
        offset += limit
        page += 1
        if isinstance(total, int) and offset >= total:
            break
        if len(rows) < limit:
            break

    return jobs


async def fetch_workable(token: str, *, save_raw: bool = True) -> list[dict]:
    """GET /api/v1/widget/accounts/{token}?details=true.

    Live shape: {"name", "description", "jobs": [...]}. Location is split across
    `city`/`state`/`country` with a richer `locations` array for multi-site reqs.
    `shortcode` is the stable id; `url` and `shortlink` are the same short link.

    Caveat worth knowing: this endpoint answers 200 with an EMPTY jobs array for
    account names that do not exist, so "200" alone never proves a real board —
    only a non-empty jobs array does. It also rate-limits aggressively, which is
    what the 429 backoff in http.py is for.
    """
    url = f"https://apply.workable.com/api/v1/widget/accounts/{token}?details=true"
    res = await polite_get(url)
    if not res.ok:
        return []
    if save_raw:
        save_fixture("workable", token, res.json)

    jobs = []
    for row in _rows(res.json, "jobs"):
        places = row.get("locations")
        if isinstance(places, list) and places:
            location = join_parts(
                [
                    join_parts([p.get("city"), p.get("region"), p.get("country")])
                    for p in places
                    if isinstance(p, dict)
                ],
                sep=" | ",
            )
        else:
            location = join_parts([row.get("city"), row.get("state"), row.get("country")])
        if not location and row.get("telecommuting"):
            location = "Remote"

        jobs.append(
            make_job(
                native_id=row.get("shortcode") or row.get("id"),
                title=row.get("title"),
                location=location,
                url=row.get("url") or row.get("shortlink") or row.get("application_url"),
                posted_at=row.get("published_on") or row.get("created_at"),
                department=row.get("department") or row.get("function"),
            )
        )
    return jobs


async def fetch_recruitee(token: str, *, save_raw: bool = True) -> list[dict]:
    """GET https://{token}.recruitee.com/api/offers/.

    Live shape: {"offers": [...]}. `location` is already a full display string;
    `locations` holds the structured list for multi-site reqs. `careers_url` is
    the public posting. Dates come back as "YYYY-MM-DD HH:MM:SS UTC", which
    iso_or_none knows how to read.
    """
    url = f"https://{token}.recruitee.com/api/offers/"
    res = await polite_get(url)
    if not res.ok:
        return []
    if save_raw:
        save_fixture("recruitee", token, res.json)

    jobs = []
    for row in _rows(res.json, "offers"):
        places = row.get("locations")
        if isinstance(places, list) and len(places) > 1:
            location = join_parts(
                [
                    join_parts([p.get("city"), p.get("state"), p.get("country")])
                    for p in places
                    if isinstance(p, dict)
                ],
                sep=" | ",
            )
        else:
            location = row.get("location") or join_parts(
                [row.get("city"), row.get("state_name"), row.get("country")]
            )
        if not location and row.get("remote"):
            location = "Remote"

        jobs.append(
            make_job(
                native_id=row.get("id") or row.get("guid"),
                title=row.get("title") or row.get("sharing_title"),
                location=location,
                url=row.get("careers_url") or row.get("careers_apply_url"),
                posted_at=row.get("published_at") or row.get("created_at"),
                department=row.get("department"),
            )
        )
    return jobs


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "smartrecruiters": fetch_smartrecruiters,
    "workable": fetch_workable,
    "recruitee": fetch_recruitee,
}


async def fetch(
    ats: str, token: str, *, save_raw: bool = True, max_pages: int | None = None
) -> list[dict]:
    """Dispatch to the right Tier 1 fetcher by ATS name.

    `max_pages` is forwarded ONLY to the one fetcher that paginates. Passing it
    for any other vendor is a no-op rather than an error: a caller sweeping a
    mixed list of boards sets it once and does not have to know which of the six
    reads more than one page.
    """
    fetcher = FETCHERS.get(ats)
    if fetcher is None:
        raise ValueError(f"unknown tier-1 ats {ats!r}; expected one of {', '.join(TIER1_ATS)}")
    if max_pages is not None and fetcher is fetch_smartrecruiters:
        return await fetcher(token, save_raw=save_raw, max_pages=max_pages)
    return await fetcher(token, save_raw=save_raw)
