"""Candidate companies, mined from GitHub.

The seed list in scripts/discovery_seed.py is a person typing names they already
know. This module is the other half: it finds names nobody typed, by looking for
the places an ATS careers URL gets written down in public.

WHAT IT ACTUALLY SEARCHES, AND WHY IT IS NOT CODE SEARCH
--------------------------------------------------------
The obvious query is GitHub's code search for "boards.greenhouse.io". That
endpoint requires authentication — unauthenticated it answers 401, flatly — so
it is used only when GITHUB_TOKEN is set.

Without a token the fallback is repository search with `in:readme`, which is
unauthenticated at 10 requests/minute. On its own that is a weak signal: it hands
back repository owners, and the owner of a repo whose README mentions Greenhouse
is almost never a company that uses Greenhouse.

But look at what those results ARE. The top hits are the community job-list
repositories — "new grad 2020", "companies sponsoring visas", "remote jobs" —
whose READMEs are tables of hundreds of careers links. So the second step is to
read those READMEs from raw.githubusercontent.com and pull the board tokens
straight out of the URLs. A token lifted from `jobs.lever.co/acme` is not a guess
that has to survive token_variants; it is the board's actual name, and the
pipeline can confirm it with one request instead of thirty.

Every host that answers here is asked politely through fetchers/http.py, and the
two GitHub endpoints get an explicit slower pace (see GITHUB_MIN_DELAY): the
search API's unauthenticated budget is ten requests a minute, which is well under
the shared 1/second default.

A failure is never fatal. GitHub being rate-limited, unreachable or hostile
returns fewer candidates — never an exception — because a discovery run that
cannot reach GitHub should still sweep the seed list.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from dotenv import load_dotenv

from .http import polite_get

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# The search API allows 10 requests/minute unauthenticated (30 with a token).
# 6.5s keeps us under the unauthenticated ceiling with room to spare; the token
# path could go faster and deliberately does not, because the gain is seconds
# and the cost of being wrong is a blocked address.
GITHUB_MIN_DELAY = 6.5
# raw.githubusercontent.com is a different budget entirely (a CDN, not the API),
# so README reads take the shared default pace rather than the search pace.
RAW_MIN_DELAY = 1.0

GITHUB_API = "https://api.github.com"
RAW_BASE = "https://raw.githubusercontent.com"

# The hostnames a careers link is written as, per ATS. `job-boards.greenhouse.io`
# is Greenhouse's newer host and appears alongside the old one in the wild.
ATS_HOSTS: dict[str, tuple[str, ...]] = {
    "greenhouse": ("boards.greenhouse.io", "job-boards.greenhouse.io"),
    "lever": ("jobs.lever.co",),
    "ashby": ("jobs.ashbyhq.com",),
    "workday": ("myworkdayjobs.com",),
}

# Board token straight out of a careers URL. Two Greenhouse spellings are in
# use: the path form and the embed form (`?for=<token>`), and both appear in
# these README tables.
_TOKEN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "greenhouse",
        re.compile(
            r"(?:boards|job-boards)\.greenhouse\.io/"
            r"(?:embed/job_board/?\?for=)?([A-Za-z0-9][A-Za-z0-9_-]{1,48})",
            re.IGNORECASE,
        ),
    ),
    ("lever", re.compile(r"jobs\.lever\.co/([A-Za-z0-9][A-Za-z0-9_-]{1,48})", re.IGNORECASE)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9][A-Za-z0-9_-]{1,48})", re.IGNORECASE)),
)

# Workday is name-only: the tenant is in the host, but the SITE is not derivable
# from anything here, so a tenant is a lead for a human rather than something the
# sweep can resolve. See the Workday note in fetchers/resolve.py.
_WORKDAY_TENANT = re.compile(
    r"(?:https?://)?([A-Za-z0-9][A-Za-z0-9_-]{1,48})\.wd\d+\.myworkdayjobs\.com",
    re.IGNORECASE,
)

# Path segments that are the vendor's own plumbing rather than anybody's board.
_NOT_A_TOKEN = frozenset(
    {
        "embed", "job_board", "jobs", "job", "boards", "board", "api", "www",
        "search", "index", "about", "careers", "career", "en", "en-us", "static",
        "assets", "img", "images", "css", "js", "favicon", "robots", "sitemap",
    }
)


@dataclass(frozen=True)
class GithubCandidate:
    """One lead. Either a bare name, or a board we can check in a single GET.

    `ats`/`token` present means the careers URL named the board outright, which
    is the whole reason this module reads READMEs rather than repo owners. Absent
    means the lead is a name and has to go through token guessing like a seed
    entry does.
    """

    name: str
    source: str
    ats: str | None = None
    token: str | None = None
    note: str | None = None


def github_token() -> str | None:
    """GITHUB_TOKEN from the environment, or None. Never required."""
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    value = os.getenv("GITHUB_TOKEN", "").strip()
    return value or None


def _headers() -> dict[str, str]:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    token = github_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def extract_boards(text: str) -> list[tuple[str, str]]:
    """Every (ats, token) pair a blob of text names, first-seen order, deduped.

    Deliberately tolerant about what surrounds the URL — these come out of
    Markdown tables, HTML anchors and bare text alike — and deliberately strict
    about the token itself: a path segment that is the vendor's own plumbing
    (`embed`, `job_board`, `api`) is not a company, and a "board" called `www`
    would be probed forever and never answer.
    """
    found: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for ats, pattern in _TOKEN_PATTERNS:
        for match in pattern.finditer(text or ""):
            token = match.group(1).strip().strip("-_").lower()
            if not token or token in _NOT_A_TOKEN or token.isdigit():
                continue
            key = (ats, token)
            if key not in seen:
                seen.add(key)
                found.append(key)
    return found


def extract_workday_tenants(text: str) -> list[str]:
    """Workday tenant names a blob of text mentions, deduped."""
    seen: list[str] = []
    for match in _WORKDAY_TENANT.finditer(text or ""):
        tenant = match.group(1).strip().lower()
        if tenant and tenant not in _NOT_A_TOKEN and tenant not in seen:
            seen.append(tenant)
    return seen


async def search_repos(hostname: str, *, per_page: int = 30, pages: int = 1) -> list[str]:
    """Repositories whose README mentions `hostname`, as `owner/repo` strings.

    Sorted by stars, because the useful hit is the widely-shared job list rather
    than someone's fork of it. Returns [] on any failure — a rate-limited search
    means fewer candidates, not a dead run.
    """
    names: list[str] = []
    for page in range(1, pages + 1):
        res = await polite_get(
            f"{GITHUB_API}/search/repositories",
            params={
                "q": f'"{hostname}" in:readme',
                "per_page": str(per_page),
                "page": str(page),
                "sort": "stars",
                "order": "desc",
            },
            min_delay=GITHUB_MIN_DELAY,
        )
        if not res.ok or not isinstance(res.json, dict):
            print(
                f"  ! github repo search for {hostname} p{page}: "
                f"{res.error or 'unusable response'}",
                file=sys.stderr,
            )
            break
        items = res.json.get("items")
        if not isinstance(items, list) or not items:
            break
        for item in items:
            full_name = (item or {}).get("full_name")
            if isinstance(full_name, str) and full_name not in names:
                names.append(full_name)
    return names


async def search_code(hostname: str, *, per_page: int = 100) -> list[str]:
    """Code-search text fragments mentioning `hostname`. Requires a token.

    Returns the matched fragments themselves rather than repository names — the
    caller wants the URLs inside them. Without GITHUB_TOKEN this returns []
    immediately rather than spending a request to be told 401.
    """
    if not github_token():
        return []

    res = await polite_get(
        f"{GITHUB_API}/search/code",
        params={"q": f'"{hostname}"', "per_page": str(per_page)},
        min_delay=GITHUB_MIN_DELAY,
    )
    if not res.ok or not isinstance(res.json, dict):
        print(f"  ! github code search for {hostname}: {res.error or 'unusable response'}",
              file=sys.stderr)
        return []

    fragments: list[str] = []
    for item in res.json.get("items") or []:
        for key in ("path", "html_url", "name"):
            value = (item or {}).get(key)
            if isinstance(value, str):
                fragments.append(value)
        for match in (item or {}).get("text_matches") or []:
            fragment = (match or {}).get("fragment")
            if isinstance(fragment, str):
                fragments.append(fragment)
    return fragments


async def fetch_readme(full_name: str) -> str | None:
    """A repository's README as text, from the raw CDN. None if unreadable.

    Tries the two conventional filenames against the default branch. Goes to
    raw.githubusercontent.com rather than /repos/{o}/{r}/readme on purpose: the
    API path spends the 60-per-hour unauthenticated core budget, and the whole
    strategy needs dozens of READMEs.
    """
    for filename in ("README.md", "readme.md", "README.rst", "README"):
        res = await polite_get(
            f"{RAW_BASE}/{full_name}/HEAD/{filename}",
            want_json=False,
            min_delay=RAW_MIN_DELAY,
        )
        if res.ok and res.text:
            return res.text
    return None


async def discover_candidates(*, limit: int = 200, repos_per_host: int = 12) -> list[GithubCandidate]:
    """Mine GitHub for candidates, newest signal first, capped at `limit`.

    The cap is the whole budget control. Board tokens found this way cost one
    verification request each in the pipeline, which is cheap — but the job-list
    repositories are large enough to yield thousands of them, and a sweep that
    never finishes is a sweep that gets interrupted.

    Order is: explicit boards from code search (token path only), then explicit
    boards from README mining, then Workday tenants as name-only leads. Explicit
    boards first because they are the ones the pipeline can settle in one GET.
    """
    boards: list[tuple[str, str]] = []
    tenants: list[str] = []
    seen_boards: set[tuple[str, str]] = set()

    def take_boards(text: str) -> None:
        for pair in extract_boards(text):
            if pair not in seen_boards:
                seen_boards.add(pair)
                boards.append(pair)

    # 1. Code search, if a token makes it available at all.
    for ats, hosts in ATS_HOSTS.items():
        if ats == "workday":
            continue
        for hostname in hosts:
            for fragment in await search_code(hostname):
                take_boards(fragment)

    # 2. README mining. The repository search is cheap; the READMEs behind it
    #    are where the volume is.
    repos: list[str] = []
    for hosts in ATS_HOSTS.values():
        for hostname in hosts:
            for full_name in await search_repos(hostname, per_page=repos_per_host):
                if full_name not in repos:
                    repos.append(full_name)

    for full_name in repos:
        if len(boards) >= limit:
            break
        text = await fetch_readme(full_name)
        if not text:
            continue
        take_boards(text)
        for tenant in extract_workday_tenants(text):
            if tenant not in tenants:
                tenants.append(tenant)

    candidates: list[GithubCandidate] = [
        GithubCandidate(name=token, source="github", ats=ats, token=token)
        for ats, token in boards[:limit]
    ]

    # Workday tenants fill whatever budget is left, as names rather than boards:
    # the sweep cannot resolve a Workday site, so these exist to be looked at by
    # a person via scripts/add_workday.py.
    room = max(0, limit - len(candidates))
    candidates.extend(
        GithubCandidate(name=tenant, source="github", note="try Workday")
        for tenant in tenants[:room]
    )
    return candidates


def summarise(candidates: Iterable[GithubCandidate]) -> str:
    """One line describing what a mining run produced."""
    items = list(candidates)
    with_board = sum(1 for c in items if c.ats and c.token)
    by_ats: dict[str, int] = {}
    for c in items:
        if c.ats:
            by_ats[c.ats] = by_ats.get(c.ats, 0) + 1
    spread = ", ".join(f"{k}={v}" for k, v in sorted(by_ats.items())) or "none"
    return (
        f"github: {len(items)} candidate(s) — {with_board} with an explicit board "
        f"({spread}), {len(items) - with_board} name-only"
    )
