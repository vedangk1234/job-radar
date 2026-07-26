"""Tier 2: Workday. One tenant per company, one paginated POST per page.

    POST https://{host}/wday/cxs/{tenant}/{site}/jobs
    {"appliedFacets": {...}, "limit": 20, "offset": 0, "searchText": ""}
    -> {"total": N, "jobPostings": [...], "facets": [...]}

Unlike Tier 1 this fetcher filters AT SOURCE, and it is the only one that does.
That is a volume decision, not a policy change: a Workday tenant like NVIDIA
carries 2000+ reqs worldwide against 212 in India, so fetching the whole board
would mean 100+ paged POSTs per company per crawl against a vendor that is
known to block. A company fetched this way is marked india_scoped in the
companies table, which is the flag that says "this row is deliberately partial".

Everything below was checked against live NVIDIA, Adobe and Salesforce tenants
rather than against notes, and four of the checks changed the code:

  1. THE COUNTRY FACET IS NOT CALLED THE SAME THING TWICE. Adobe exposes
     `locationCountry`; NVIDIA has no country facet at all and carries India as
     a value of `locationHierarchy1`; Salesforce calls it
     `CF_-_REC_-_LRV_-_Job_Posting_Anchor_-_Country_from_Job_Posting_Location_Extended`.
     So discovery works by VALUE ("which facet has a value called India?") and
     never by facet name.

  2. NEVER SUBSTRING-MATCH "india". Salesforce's `locations` facet lists
     "Indiana - Indianapolis", "Indiana - Remote" and "Indiana - Chicago Metro".
     A contains-check would quietly scope a company to Indiana, USA. Values are
     matched on an EXACT descriptor of "India"; that one rule picks the right
     facet on all three tenants and cannot pick Indiana on any of them.

  3. AN UNKNOWN FACET VALUE IS IGNORED, NOT REJECTED. Posting Adobe a country
     WID it does not know answers 200 with 545 San Jose jobs — a near-unfiltered
     board wearing the costume of a filtered one. That is why the hardcoded
     INDIA_WID fallback below is never trusted on its own: it is applied, then
     checked for having actually narrowed anything, and dropped if it did not.

  4. OFFSET PAST `total` WRAPS TO PAGE 1. Asking NVIDIA for offset=10000 of 212
     returns 200 and the same twenty rows as offset=0. A "stop when the page
     comes back empty" loop would therefore never stop. Paging stops on
     `offset >= total`, on a hard page cap, and on a page that contributes no
     new ids — three independent stops, because the natural one does not exist.

FAILURE SEMANTICS (the Workable lesson). total=0 on a well-formed response is a
real, empty board and returns []. A 404, an HTML error page, or a response with
no `total`/`jobPostings` RAISES, so Guard 1 in the crawl loop leaves the jobs
table untouched. An error is never allowed to look like "every job closed".
"""

from __future__ import annotations

import hashlib
import re
import sys
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from .fixtures import save_fixture
from .http import polite_post
from .normalize import city_label, join_parts, make_job

# limit=100 is rejected with HTTP 400 by every tenant tested. 20 is the cap.
PAGE_SIZE = 20

# Workday is block-sensitive, so it gets a gentler pace than the global 1.0s.
# _pace takes the larger of the two, so this can only ever slow us down.
PER_HOST_DELAY = 2.0

# Unfiltered fallback only (no India facet on the tenant). At 20 rows a page,
# 25 pages is 500 jobs and 25 POSTs — roughly a minute of polite paging, and
# far short of a 2000-req board. We would rather return a capped, honestly
# logged slice than hammer a vendor that has not offered us a filter.
MAX_PAGES_UNFILTERED = 25
MAX_JOBS_UNFILTERED = 500

# A filtered board should be small. This is a runaway stop, not a real limit:
# 200 pages is 4000 India jobs, which no tenant we track comes close to.
MAX_PAGES_FILTERED = 200

# Widely-cited Workday country WID for India. It is a LAST resort and is never
# trusted blind — see note 3 in the module docstring, and _facet_narrows().
# It matched none of NVIDIA, Adobe or Salesforce, so in practice discovery is
# what does the work; this only covers a tenant that hides its facet list.
INDIA_WID = "bc33aa3152ec42d4995f4791a106ed09"

# Facet parameters worth trying the blind WID against, best first.
FALLBACK_FACET_PARAMS = ("locationCountry", "locationHierarchy1")

HEADERS = {"Content-Type": "application/json", "Accept": "application/json"}

# "…/Senior-Data-Scientist_JR353414" -> JR353414
# "…/Accountant_JR349380-1"          -> JR349380-1  (Workday's own disambiguator;
#                                       kept, because it is what makes the URL
#                                       unique and two live postings can share a
#                                       bare req id)
_REQ_ID = re.compile(r"_([A-Za-z0-9][A-Za-z0-9._-]*)$")

# locationsText is a count, not a place, on ~26% of NVIDIA's India rows:
# "2 Locations", "3 Locations". Those label as `other` and would drop out of
# the funnel, so they get the externalPath's own location segment folded in.
_COUNT_ONLY = re.compile(r"^\s*\d+\s+locations?\s*$", re.IGNORECASE)


class WorkdayError(RuntimeError):
    """The tenant did not answer with a board. Raised so Guard 1 sees it."""


# --- facet discovery -------------------------------------------------------


def _walk_facets(nodes: Any):
    """Yield every facet dict in the tree, including nested groups.

    `locationMainGroup` is a group whose `values` are themselves facets rather
    than selectable values, so a flat scan of the top level misses the country
    facet on every tenant that uses one.
    """
    if not isinstance(nodes, list):
        return
    for node in nodes:
        if not isinstance(node, dict):
            continue
        values = node.get("values")
        nested = [v for v in values if isinstance(v, dict) and v.get("facetParameter")] if isinstance(values, list) else []
        if nested:
            yield from _walk_facets(nested)
        else:
            yield node


def _is_india(descriptor: Any) -> bool:
    """Exact match only. See note 2: "Indiana" is a US state, not a typo."""
    return str(descriptor or "").strip().casefold() == "india"


def find_india_facet(payload: Any) -> tuple[str, str] | None:
    """(facetParameter, india_wid) discovered from the tenant's own facets.

    Prefers a facet that calls itself Country — on a tenant that has both, the
    country facet is the one that means "the job is in India" rather than "the
    job is at a site whose name happens to start with India".
    """
    candidates: list[tuple[int, str, str]] = []
    for facet in _walk_facets(payload.get("facets") if isinstance(payload, dict) else None):
        param = facet.get("facetParameter")
        if not isinstance(param, str) or not param:
            continue
        for value in facet.get("values") or []:
            if not isinstance(value, dict) or not _is_india(value.get("descriptor")):
                continue
            looks_country = (
                "country" in str(facet.get("descriptor") or "").casefold()
                or "country" in param.casefold()
            )
            candidates.append((0 if looks_country else 1, param, str(value.get("id") or "")))
    candidates = [c for c in candidates if c[2]]
    if not candidates:
        return None
    _, param, wid = min(candidates, key=lambda c: c[0])
    return param, wid


# --- mapping ---------------------------------------------------------------


def _path_location(external_path: str | None) -> str | None:
    """The location segment of /job/{Location}/{Title}_{REQ}, de-slugified.

    Workday slugifies a location by turning punctuation into dashes, so
    "India - Bangalore" arrives as "India---Bangalore" and "India, Bengaluru"
    as "India-Bengaluru". Undoing that exactly is not possible and not needed —
    city_label only wants the city token to survive.
    """
    segments = [s for s in str(external_path or "").split("/") if s]
    if len(segments) < 3 or segments[0].casefold() != "job":
        return None
    slug = segments[1].replace("---", " - ").replace("-", " ")
    return re.sub(r"\s+", " ", slug).strip() or None


def parse_native_id(external_path: Any) -> str | None:
    """The req id at the end of externalPath, else a hash of the path.

    Hashing rather than returning None matters: canonical_id would otherwise
    fall back to hashing title+location, and a Workday board carries several
    reqs that share both (same role, same city, different req).
    """
    path = str(external_path or "").strip().rstrip("/")
    if not path:
        return None
    match = _REQ_ID.search(path)
    if match:
        return match.group(1)
    return "wd-" + hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]


def build_url(host: str, site: str, external_path: Any, lang: str | None = "en-US") -> str | None:
    """https://{host}/{lang}/{site}{externalPath}.

    Verified live against NVIDIA, Adobe and Salesforce: both this form and the
    lang-less one answer 200 and render the posting. The lang form is the one
    Workday's own careers site links to, so it is the one we store.
    """
    path = str(external_path or "").strip()
    if not host or not site or not path:
        return None
    if not path.startswith("/"):
        path = "/" + path
    prefix = f"/{quote(lang.strip('/'))}" if lang and lang.strip("/") else ""
    return f"https://{host}{prefix}/{quote(site.strip('/'))}{path}"


def _department(posting: dict) -> Any:
    """jobFamily when the tenant sends one; never the req id.

    `bulletFields` is documented as the extra columns of the results table, and
    on every tenant checked it holds exactly one value: the req id, which is
    already native_id. Writing that into `department` would fill the column with
    "JR2019348", so bulletFields is only used when it carries something that is
    NOT the id we just parsed from the path.
    """
    for key in ("jobFamily", "jobFamilyGroup", "jobRequisitionCategory"):
        value = posting.get(key)
        if isinstance(value, dict):
            value = value.get("descriptor") or value.get("label")
        if isinstance(value, str) and value.strip():
            return value

    own_id = parse_native_id(posting.get("externalPath"))
    for bullet in posting.get("bulletFields") or []:
        text = str(bullet or "").strip()
        if text and text != own_id and not (own_id or "").startswith(text):
            return text
    return None


def map_posting(posting: dict, host: str, site: str, lang: str | None) -> dict:
    """One jobPosting -> the canonical row shape.

    posted_at is hardcoded None on purpose. Workday's `postedOn` is a relative
    English phrase ("Posted 30+ Days Ago"), which is not a date and cannot be
    turned into one without inventing precision. first_seen_at — our own
    observation — is the timestamp this project trusts anyway.
    """
    external_path = posting.get("externalPath")
    location = posting.get("locationsText")
    if not location or _COUNT_ONLY.match(str(location)):
        # Keep the vendor's own string AND add the path's city, so a
        # "3 Locations" row can still be labelled instead of dropped.
        location = join_parts([_path_location(external_path), location], sep=" | ")

    return make_job(
        native_id=parse_native_id(external_path),
        title=posting.get("title"),
        location=location,
        url=build_url(host, site, external_path, lang),
        posted_at=None,
        department=_department(posting),
    )


# --- fetching --------------------------------------------------------------


def _page_body(facets: dict, offset: int) -> dict:
    return {
        "appliedFacets": facets,
        "limit": PAGE_SIZE,
        "offset": offset,
        "searchText": "",
    }


def _check(res, url: str, offset: int) -> dict:
    """Turn a Fetched into a validated payload, or raise.

    This is the whole failure contract in one place: anything that is not a
    well-formed board raises, so the crawl loop's Guard 1 changes nothing. A
    well-formed board with total=0 is NOT an error and gets through.
    """
    if not res.ok:
        raise WorkdayError(f"{url} offset={offset}: {res.error or f'HTTP {res.status}'}")
    payload = res.json
    if not isinstance(payload, dict):
        raise WorkdayError(f"{url} offset={offset}: response was not a JSON object")
    if "total" not in payload and "jobPostings" not in payload:
        # An HTML error page that happens to parse, or a Workday error envelope
        # ({"errorCode": ...}) — either way it is not a board.
        raise WorkdayError(
            f"{url} offset={offset}: no total/jobPostings in response "
            f"(keys: {sorted(payload)[:6]})"
        )
    if not isinstance(payload.get("jobPostings", []), list):
        raise WorkdayError(f"{url} offset={offset}: jobPostings was not a list")
    return payload


async def _fetch_page(url: str, facets: dict, offset: int) -> dict:
    res = await polite_post(
        url,
        json_body=_page_body(facets, offset),
        headers=HEADERS,
        min_delay=PER_HOST_DELAY,
    )
    return _check(res, url, offset)


def _india_share(postings: list, host: str, site: str, lang: str | None) -> float:
    """Fraction of a page that labels as somewhere in India."""
    rows = [map_posting(p, host, site, lang) for p in postings if isinstance(p, dict)]
    if not rows:
        return 0.0
    hits = sum(1 for r in rows if city_label(r["location"]) != "other")
    return hits / len(rows)


async def _blind_facet(
    url: str, unfiltered_total: int, host: str, site: str, lang: str | None
) -> tuple[dict, str] | None:
    """Last resort: try INDIA_WID and keep it only if it demonstrably filters.

    Two things have to hold before we believe it, because Workday answers 200
    for a facet value it does not recognise: the total must actually drop, and
    the first page must actually look Indian. Either check failing means the
    facet did nothing and we must not claim india_scoped.
    """
    for param in FALLBACK_FACET_PARAMS:
        facets = {param: [INDIA_WID]}
        try:
            payload = await _fetch_page(url, facets, 0)
        except WorkdayError:
            continue
        total = payload.get("total")
        if not isinstance(total, int) or total <= 0 or total >= unfiltered_total:
            continue
        if _india_share(payload.get("jobPostings") or [], host, site, lang) < 0.5:
            continue
        return facets, param
    return None


@dataclass
class WorkdayFetch:
    """A fetch plus the two facts about it that only the fetch itself knows."""

    rows: list[dict]
    scoped: bool  # the India facet was applied at source -> india_scoped=TRUE
    facet: str | None = None  # which facet parameter did it
    board_total: int = 0  # `total` the tenant reported for the query we ran


async def fetch_workday(token: str, config: dict | None = None, *, save_raw: bool = True) -> list[dict]:
    """Every India job on one Workday tenant, in the canonical row shape.

    This is the signature the crawl loop's FETCHERS table calls. Callers that
    need to know whether the source-side filter was applied (the seeding script,
    which sets companies.india_scoped) should call fetch_workday_detailed.
    """
    return (await fetch_workday_detailed(token, config, save_raw=save_raw)).rows


async def fetch_workday_detailed(
    token: str, config: dict | None = None, *, save_raw: bool = True
) -> WorkdayFetch:
    """fetch_workday, plus whether the India facet was actually applied.

    `token` is the tenant. `config` carries {host, site, lang?, careers_url?} —
    see fetchers.resolve.parse_workday_url, which builds it from a pasted
    careers URL.

    Raises WorkdayError when the tenant does not answer with a board. Returns an
    empty row list only when the board really is empty.
    """
    config = config or {}
    host = str(config.get("host") or "").strip().rstrip("/")
    site = str(config.get("site") or "").strip().strip("/")
    lang = config.get("lang", "en-US")
    if not host or not site:
        raise WorkdayError(
            f"workday tenant {token!r} needs config host and site; got {config!r}"
        )

    url = f"https://{host}/wday/cxs/{token}/{site}/jobs"

    # One unfiltered call, for the facet list and for the baseline total that
    # tells us whether a fallback facet actually did anything.
    first = await _fetch_page(url, {}, 0)
    if save_raw:
        save_fixture("workday", token, first, suffix="probe")

    unfiltered_total = first.get("total")
    unfiltered_total = unfiltered_total if isinstance(unfiltered_total, int) else 0

    found = find_india_facet(first)
    facets: dict = {}
    scoped = False
    facet_used: str | None = None
    if found:
        facet_used, wid = found
        facets = {facet_used: [wid]}
        scoped = True
        print(f"  workday:{token} India facet {facet_used}={wid}")
    else:
        blind = await _blind_facet(url, unfiltered_total, host, site, lang)
        if blind:
            facets, facet_used = blind
            scoped = True
            print(f"  workday:{token} India facet {facet_used}=<fallback WID>, verified to narrow")
        else:
            print(
                f"  workday:{token} NO India facet on this tenant — falling back to an "
                f"unfiltered fetch capped at {MAX_JOBS_UNFILTERED} jobs / "
                f"{MAX_PAGES_UNFILTERED} pages, filtered client-side by city label",
                file=sys.stderr,
            )

    max_pages = MAX_PAGES_FILTERED if scoped else MAX_PAGES_UNFILTERED
    jobs: dict[str, dict] = {}
    offset, page, total = 0, 0, None

    while page < max_pages:
        payload = first if (page == 0 and not facets) else await _fetch_page(url, facets, offset)
        if save_raw and not (page == 0 and not facets):
            save_fixture("workday", token, payload, suffix=f"p{page}")

        if total is None:
            reported = payload.get("total")
            total = reported if isinstance(reported, int) else 0

        postings = [p for p in payload.get("jobPostings") or [] if isinstance(p, dict)]
        if not postings:
            break

        # Offset past `total` wraps to page 1 (note 4), so a page that adds
        # nothing new is a wrap, not more data. This is the stop that keeps a
        # mis-reported `total` from spinning forever.
        before = len(jobs)
        for posting in postings:
            row = map_posting(posting, host, site, lang)
            key = row["url"] or row["native_id"] or str(posting)
            jobs.setdefault(key, row)
        if len(jobs) == before:
            break

        offset += PAGE_SIZE
        page += 1
        if offset >= total:
            break
        if not scoped and len(jobs) >= MAX_JOBS_UNFILTERED:
            break

    rows = list(jobs.values())

    if not scoped:
        # No source-side filter, so filter here. `other` is the only label that
        # is confidently not-India; india-unclear stays in, as everywhere else.
        kept = [r for r in rows if city_label(r["location"]) != "other"]
        print(
            f"  workday:{token} unfiltered fallback kept {len(kept)}/{len(rows)} "
            f"fetched row(s) by city label (board total {total})",
            file=sys.stderr,
        )
        rows = kept

    return WorkdayFetch(rows=rows, scoped=scoped, facet=facet_used, board_total=total or 0)
