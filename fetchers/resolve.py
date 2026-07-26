"""Guess which ATS a company uses, by trying board tokens until one answers.

Board tokens are almost always the company name with the spaces taken out, so
this tries that first and then a short list of mechanical variants. It is a
cheap heuristic, not a lookup service: a company whose token is genuinely
unguessable (a registered legal name, say) will not resolve, and that is the
honest answer. Unresolved companies get their token filled in by hand later.

Order is greenhouse -> lever -> ashby, and the first board that answers with a
NON-EMPTY job list wins. Non-empty matters: some vendors answer 200 with an
empty array for tokens that do not exist, so a bare 200 proves nothing.

Probes never write fixtures. The caller re-fetches the board it settles on and
saves that response, so the archive holds one clean fixture per real board
rather than one per guess.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Coroutine
from urllib.parse import urlsplit

from .normalize import norm
from .tier1 import fetch_ashby, fetch_greenhouse, fetch_lever

RESOLVE_ORDER: tuple[tuple[str, Callable[..., Coroutine]], ...] = (
    ("greenhouse", fetch_greenhouse),
    ("lever", fetch_lever),
    ("ashby", fetch_ashby),
)

# Tokens that no mechanical rule produces, confirmed by hand against the live
# board. Kept deliberately tiny — this is an escape hatch, not a directory.
ALIASES: dict[str, tuple[str, ...]] = {
    "razorpay": ("razorpaysoftwareprivatelimited",),
}


def token_variants(company_name: str) -> list[str]:
    """Candidate board tokens for a company name, best guess first.

    The base is norm(name) with spaces removed. The variants cover the two
    habits that account for most misses: a suffix bolted onto the name, and a
    hyphenated slug instead of a squashed one.
    """
    normalised = norm(company_name)
    base = normalised.replace(" ", "")
    if not base:
        return []

    variants = [base]
    hyphenated = normalised.replace(" ", "-")
    if hyphenated != base:
        variants.append(hyphenated)
    variants.extend(f"{base}{suffix}" for suffix in ("inc", "hq", "technologies"))
    variants.extend(ALIASES.get(base, ()))

    seen, ordered = set(), []
    for token in variants:
        if token not in seen:
            seen.add(token)
            ordered.append(token)
    return ordered


async def resolve(company_name: str) -> tuple[str, str, int] | None:
    """Return (ats, token, job_count) for the first live board, else None."""
    for token in token_variants(company_name):
        for ats, fetcher in RESOLVE_ORDER:
            jobs = await fetcher(token, save_raw=False)
            if jobs:
                return ats, token, len(jobs)
    return None


# --- Workday -------------------------------------------------------------
#
# Workday is not guessable the way a Tier 1 board token is: the tenant, the host
# and the site name are three independent strings, and the site name is whatever
# the company's HR team typed ("NVIDIAExternalCareerSite", "external_experienced",
# "External_Career_Site"). So there is no probing here — you paste the careers
# URL you are looking at and this takes it apart.

# nvidia.wd5.myworkdayjobs.com -> ("nvidia", "wd5")
_WD_HOST = re.compile(r"^(?P<tenant>[^.]+)\.(?P<pod>wd\d+)\.myworkdayjobs\.com$", re.IGNORECASE)

# A path segment is a language tag, not a site name, when it looks like en-US
# or en. Workday puts it before the site: /en-US/NVIDIAExternalCareerSite.
_LANG = re.compile(r"^[a-z]{2}(?:[-_][A-Za-z]{2,4})?$")

# Segments that are part of the API/app plumbing rather than the site name.
_NOT_A_SITE = {"wday", "cxs", "jobs", "job", "d", "recruiting", "en"}

DEFAULT_LANG = "en-US"


def parse_workday_url(careers_url: str) -> dict[str, Any]:
    """A pasted Workday careers URL -> the config dict fetch_workday wants.

    Returns {host, tenant, site, lang, careers_url} and, when something had to
    be guessed, an `ambiguous` list naming what. Handles:

        https://{tenant}.wd{N}.myworkdayjobs.com/{lang}/{site}
        https://{tenant}.wd{N}.myworkdayjobs.com/{site}
        https://{vanity-host}/{lang}/{site}          (custom domain)
        .../{lang}/{site}/job/{Location}/{Title}_{REQ}   (a posting URL)

    Never raises on a URL it cannot fully understand: it returns what it did
    parse with `ambiguous` set, because a half-parsed config a human can correct
    is more useful than an exception. A caller should refuse to store a config
    whose `ambiguous` list is non-empty without a human looking at it.
    """
    raw = str(careers_url or "").strip()
    if raw and "//" not in raw:
        raw = "https://" + raw  # a pasted host with no scheme

    parts = urlsplit(raw)
    host = (parts.netloc or "").split("@")[-1].strip().rstrip("/").lower()
    segments = [s for s in (parts.path or "").split("/") if s]
    ambiguous: list[str] = []

    # A posting URL carries the site in front of /job/...; drop the posting.
    for cut in ("job", "d"):
        if cut in segments:
            segments = segments[: segments.index(cut)]
            break
    # An API URL: /wday/cxs/{tenant}/{site}/jobs
    if len(segments) >= 4 and segments[0].lower() == "wday" and segments[1].lower() == "cxs":
        return {
            "host": host,
            "tenant": segments[2],
            "site": segments[3],
            "lang": DEFAULT_LANG,
            "careers_url": f"https://{host}/{DEFAULT_LANG}/{segments[3]}",
        }

    lang = None
    if segments and _LANG.match(segments[0]):
        lang = segments[0]
        segments = segments[1:]

    site = next((s for s in segments if s.lower() not in _NOT_A_SITE), None)
    if not site:
        ambiguous.append("site")

    match = _WD_HOST.match(host)
    if match:
        tenant = match.group("tenant")
    else:
        # Custom/vanity domain: the tenant is not in the host. The first label
        # is the best available guess ("careers.acme.com" -> "careers" is wrong,
        # "acme.wd3.custom.com" -> "acme" is right), so flag it either way and
        # let a human confirm against the /wday/cxs/ URL the site itself calls.
        labels = [l for l in host.split(".") if l]
        tenant = labels[0] if labels else None
        if not host:
            ambiguous.append("host")
        if tenant:
            ambiguous.append("tenant")
        else:
            ambiguous.append("tenant")

    config: dict[str, Any] = {
        "host": host or None,
        "tenant": tenant,
        "site": site,
        "lang": lang or DEFAULT_LANG,
        "careers_url": raw or None,
    }
    if ambiguous:
        config["ambiguous"] = ambiguous
    return config
