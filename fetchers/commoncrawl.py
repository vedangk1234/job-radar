"""Board tokens, mined out of the Common Crawl URL index.

The third candidate source, alongside the hand-typed seed list and GitHub
mining. Same shape as fetchers/discovery.py — it reads public URLs and pulls
`(ats, token)` pairs straight out of them — and the same honesty about what a
URL can and cannot tell you.

WHAT THIS TALKS TO
------------------
`https://index.commoncrawl.org` only: a free, public, unauthenticated HTTP
endpoint. No AWS, no Athena, no S3, no account, no API key. Every request goes
through the same fetchers/http.py politeness the rest of the crawler uses.

The index is queried per ATS host with a URL prefix, and it answers with the
URLs it saw. `boards.greenhouse.io/acme/jobs/123?utm_source=x` is not a guess
that has to survive token_variants — the board's real name is sitting in the
first path segment, exactly as it is in a README careers link.

WHY THE HARVESTABLE SET IS FIVE ATSes AND NOT NINE
--------------------------------------------------
Two exclusions, for two different reasons, and neither is a preference:

  * WORKDAY is not in here at all. A Workday board is a tenant, a pod host and
    a site name — three independent unguessable strings — and the URL carries
    at most the first. This is the same reason the discovery sweep never
    attempts Workday (see fetchers/resolve.py), not a new decision.

  * LEVER is queried and returns nothing, ON PURPOSE. jobs.lever.co/robots.txt
    carries `User-agent: CCBot / Disallow: /`, and Common Crawl obeys it, so
    the index holds Lever's robots.txt and not one board URL. The query is kept
    as a TRIPWIRE rather than deleted: it costs one request per run, it reports
    "0 boards — CCBot still disallowed", and the day Lever unblocks CCBot the
    harvest notices by itself. Deleting it would mean somebody re-discovers the
    block from scratch in a year.

WHAT THIS DOES NOT DO
---------------------
It does not fetch a board, does not verify a token, and does not decide whether
a company is Indian. The index is a list of URLs; it carries no page content,
so nothing here can know what a board posts or where. Everything harvested is
stored and verified later by the two-bar test. See `india_hint` for the one
weak, clearly-labelled guess this module does make.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from .discovery import _NOT_A_TOKEN
from .http import polite_get

CDX_BASE = "https://index.commoncrawl.org"
COLLINFO_URL = f"{CDX_BASE}/collinfo.json"

# The index answers 502 under repeated querying — observed, not theoretical.
# http.py already retries 5xx with jittered backoff; this is the pace that
# keeps us from provoking it in the first place. A whole harvest is ~13
# requests, so five seconds apiece costs about a minute.
CC_MIN_DELAY = 5.0

# Only the URL field. The full CDX record is ~700 bytes (digest, offset, WARC
# filename, mime, length); the URL alone is ~70. Across a harvest that is the
# difference between ~90 MB and ~9 MB, for the one field this module reads.
CDX_FIELDS = "url"

# A board token, once lowercased. The trailing shape guard is what rejects
# `robots.txt` and `sitemap.xml` — a dot cannot appear in a token.
_TOKEN_SHAPE = re.compile(r"[a-z0-9][a-z0-9_-]{1,48}")


@dataclass(frozen=True)
class Target:
    """One CDX query, and how to read a token out of what it returns.

    `ats` is the value that lands in discovered_companies, so two Greenhouse
    hosts are two Targets carrying the same `ats` on purpose — they are one
    vendor reached by two names, and their tokens dedupe against each other.
    """

    ats: str
    url: str  # the CDX `url=` parameter
    match_type: str | None = None  # "domain" for a wildcard-subdomain vendor
    kind: str = "path"  # "path": first path segment | "subdomain": host label
    suffix: str = ""  # for kind="subdomain": the host tail to strip
    embed_param: str | None = None  # a query param that names the board instead
    expect_empty: str | None = None  # why this target legitimately yields zero

    @property
    def label(self) -> str:
        return f"{self.ats}:{self.url}"


# Every host a supported board is written as, in report order.
#
# Both Greenhouse hosts are queried, though the old one is currently redundant.
# boards.greenhouse.io now 301-redirects to job-boards.greenhouse.io, and a
# full harvest of CC-MAIN-2026-30 measured its 12,075 captures as contributing
# ZERO tokens the new host did not already carry. It is kept anyway, for one
# request a run: it is still a live redirect origin, its captures still name
# their boards in the URL, and "subsumed this month" is not "subsumed forever".
# The per-ATS report makes its contribution visible, so if it stays at zero for
# several crawls the evidence to drop it will be sitting in the run log.
TARGETS: tuple[Target, ...] = (
    Target("greenhouse", "job-boards.greenhouse.io/*", embed_param="for"),
    Target("greenhouse", "boards.greenhouse.io/*", embed_param="for"),
    Target("ashby", "jobs.ashbyhq.com/*"),
    Target("smartrecruiters", "jobs.smartrecruiters.com/*"),
    Target("workable", "apply.workable.com/*"),
    # Recruitee gives each customer a subdomain rather than a path, so the
    # prefix query has nothing to bite on. matchType=domain sweeps every
    # *.recruitee.com host and the token is the leftmost label.
    Target(
        "recruitee",
        "recruitee.com",
        match_type="domain",
        kind="subdomain",
        suffix=".recruitee.com",
    ),
    # THE TRIPWIRE. This returns zero boards today and that is the correct,
    # expected result — jobs.lever.co/robots.txt says `User-agent: CCBot /
    # Disallow: /`, so Common Crawl never fetched a single Lever board page and
    # the index holds only Lever's robots.txt itself. It is queried anyway so a
    # harvest REPORTS the block instead of quietly omitting a vendor, and so
    # the run that first returns a non-zero count is the day we learn Lever
    # unblocked CCBot. One request per run buys that.
    Target(
        "lever",
        "jobs.lever.co/*",
        expect_empty="CCBot still disallowed by robots.txt",
    ),
)


# --- the India hint -------------------------------------------------------
#
# A GUESS, and a weak one. It gates nothing: every token harvested is stored
# whether or not this fires. Real India verification is the two-bar test run
# later against the live board, which is the only thing that can actually know.
#
# It has to be weak, because of what the data is. Common Crawl indexes URLs,
# not page content, and every URL here is on the ATS's own host — so there is
# no `.in` domain to read and no job text to search. The token string is the
# only signal that exists, and most companies do not put their country in it.
#
# Measured against a full CC-MAIN-2026-30 harvest: 11 of 11,474 tokens fired —
# 0.10% — and one of those eleven (`indiancreekschool`) is an American school.
# PhonePe and Meesho are both in the harvest and neither is flagged. Treat a
# True here as "worth a human glance sooner", never as "this board has India
# jobs".
_INDIA_MARKERS = (
    "india", "bharat", "bengaluru", "bangalore", "hyderabad",
    "gurugram", "gurgaon", "noida",
)
_MARKER_RE = "|".join(_INDIA_MARKERS)

# Anchored deliberately: starts with, ends with, or is a hyphen/underscore
# delimited word. A bare substring search is what flags `codesignal`,
# `cannondesign` and `andesite` on the "desi" in "design" — measured, not
# hypothetical, which is why the anchors are here.
_INDIA_HINT = re.compile(
    rf"(?:^(?P<start>{_MARKER_RE}))"
    rf"|(?:(?P<end>{_MARKER_RE})$)"
    rf"|(?:(?<=[-_])(?P<word>{_MARKER_RE})(?=$|[-_]))",
    re.IGNORECASE,
)


def india_hint(token: str) -> str | None:
    """Why this token *might* be an Indian board, or None. A guess, not a test.

    Returns a short human-readable reason so the stored flag carries its own
    caveat — a person reading discovered_companies can see the rule that fired
    and judge it, rather than trusting a bare boolean.
    """
    match = _INDIA_HINT.search(token or "")
    if not match:
        return None
    if match.group("start"):
        return f"token starts with {match.group('start')!r} (guess)"
    if match.group("end"):
        return f"token ends with {match.group('end')!r} (guess)"
    return f"token contains the word {match.group('word')!r} (guess)"


# --- token extraction ------------------------------------------------------


def token_from_url(target: Target, url: str) -> str | None:
    """The board token a matched URL names, or None if it names none.

    Normalised to be BYTE-IDENTICAL to what fetchers/discovery.py lifts out of
    a README careers link — lowercased, stripped of surrounding `-`/`_`, and
    rejected against the same `_NOT_A_TOKEN` set. That is not tidiness: the
    dedupe key is `(ats, token)` and a token that normalises differently here
    would be stored a second time and re-probed forever.

    The junk this has to survive is real and was measured against the live
    index: query strings (`?utm_source=…`, `?gh_jid=…`), deeper paths
    (`/jobs/4644852002`), trailing slashes, the Greenhouse embed form
    (`/embed/job_board?for=acme`), vendor plumbing (`/j/<code>` shortlinks —
    1,150 of them on Workable alone), and `robots.txt` at the host root.
    """
    parts = urlsplit(url or "")
    raw: str | None = None

    if target.embed_param:
        # The embed form names the board in the query string and puts vendor
        # plumbing in the path, so it has to be read before the path is.
        values = parse_qs(parts.query).get(target.embed_param)
        if values:
            raw = values[0]

    if raw is None:
        if target.kind == "subdomain":
            host = parts.netloc.lower().split(":")[0].split("@")[-1]
            if host.endswith(target.suffix):
                label = host[: -len(target.suffix)]
                # `www.acme.recruitee.com` is not a board called `www.acme`,
                # and a bare `recruitee.com` has no label at all.
                if label and "." not in label:
                    raw = label
        else:
            segments = [s for s in parts.path.split("/") if s]
            if segments:
                raw = segments[0]

    if raw is None:
        return None

    token = raw.strip().strip("-_").lower()
    if not token or token in _NOT_A_TOKEN or token.isdigit():
        return None
    if not _TOKEN_SHAPE.fullmatch(token):
        return None
    return token


# --- the CDX index ---------------------------------------------------------


def _lines(text: str | None) -> list[dict]:
    """Parse a CDX response body.

    The index answers JSON-LINES, not JSON: one object per line, no enclosing
    array. `json.loads` on the whole body fails, which is why every call here
    uses `want_json=False` and parses line by line. A malformed line is skipped
    rather than fatal — a truncated last line must not discard the page.
    """
    rows: list[dict] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            rows.append(parsed)
    return rows


def _params(crawl_id: str, target: Target, **extra: str) -> dict[str, str]:
    params = {"url": target.url, "output": "json", "fl": CDX_FIELDS}
    if target.match_type:
        params["matchType"] = target.match_type
    params.update(extra)
    return params


def index_url(crawl_id: str) -> str:
    return f"{CDX_BASE}/{crawl_id}-index"


async def latest_crawl_id() -> str | None:
    """The newest monthly crawl id, e.g. 'CC-MAIN-2026-30'. None if unreadable.

    Read from collinfo.json rather than hardcoded, so re-running on next
    month's crawl is a re-run and not an edit. `--crawl-id` overrides it.
    """
    res = await polite_get(COLLINFO_URL, min_delay=CC_MIN_DELAY)
    if not res.ok or not isinstance(res.json, list) or not res.json:
        return None
    newest = res.json[0]
    identifier = (newest or {}).get("id")
    return identifier if isinstance(identifier, str) else None


async def num_pages(crawl_id: str, target: Target) -> int:
    """How many CDX pages this target spans. 0 when the index has nothing.

    A prefix the index has never seen answers 404 with a text body, which is an
    answer rather than a failure — it means no boards, and the caller reports
    zero for that vendor instead of dying.
    """
    res = await polite_get(
        index_url(crawl_id),
        want_json=False,
        params=_params(crawl_id, target, showNumPages="true"),
        min_delay=CC_MIN_DELAY,
    )
    if not res.ok:
        return 0
    rows = _lines(res.text)
    if not rows:
        return 0
    pages = rows[0].get("pages")
    return pages if isinstance(pages, int) and pages > 0 else 0


async def fetch_page(crawl_id: str, target: Target, page: int) -> list[str]:
    """Every URL the index holds for this target on this page.

    NOTHING IS FILTERED BY HTTP STATUS, deliberately. Every record under
    `boards.greenhouse.io` is a 301 redirect stub pointing at the newer host,
    so filtering to 200 would drop that whole host — and would quietly discard
    redirect captures on every other vendor too. The token is in the URL
    whether the capture was a page or a redirect, and this module is harvesting
    names, not reading content, so the status code is not its business.
    """
    res = await polite_get(
        index_url(crawl_id),
        want_json=False,
        params=_params(crawl_id, target, page=str(page)),
        min_delay=CC_MIN_DELAY,
    )
    if not res.ok:
        return []
    return [row["url"] for row in _lines(res.text) if isinstance(row.get("url"), str)]


def tokens_from_urls(target: Target, urls: list[str]) -> list[str]:
    """Distinct tokens in a page of URLs, first-seen order preserved."""
    seen: set[str] = set()
    found: list[str] = []
    for url in urls:
        token = token_from_url(target, url)
        if token and token not in seen:
            seen.add(token)
            found.append(token)
    return found
