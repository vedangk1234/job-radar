"""Tier 3: generic handlers for careers pages that run on no known ATS.

These are best-effort by definition — they read whatever structure a site
happens to expose. Both return [] rather than raising when a page turns out to
have nothing usable, because a Tier 3 site going dark must not break a crawl
that also covers thirty Tier 1 boards.

Like Tier 1, these fetch everything they find and filter nothing.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urljoin, urlsplit

from .fixtures import save_fixture
from .http import polite_get
from .normalize import join_parts, make_job

_LDJSON_BLOCK = re.compile(
    r"<script[^>]+type\s*=\s*[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)
_LOC_TAG = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.IGNORECASE | re.DOTALL)
_URL_ENTRY = re.compile(r"<url\b.*?</url>", re.IGNORECASE | re.DOTALL)
_LASTMOD_TAG = re.compile(r"<lastmod>\s*(.*?)\s*</lastmod>", re.IGNORECASE | re.DOTALL)
_SITEMAPINDEX = re.compile(r"<sitemapindex\b", re.IGNORECASE)

# A posting URL is one whose path contains a jobs-ish SEGMENT with a slug after
# it: remoteok.com/remote-jobs/<slug>, acme.com/careers/<slug>. Segment matching
# (rather than a substring hint) is what keeps a blog post at
# /insights/blog/career-pathways-... out, while still catching "remote-jobs".
_JOB_SEGMENT = re.compile(
    r"^(?:[a-z0-9]+-)*(?:jobs?|careers?|vacanc(?:y|ies)|openings?|positions?|roles?)$"
)

# Child sitemaps inside an index are named, not pathed — "sitemap-jobs-3.xml",
# not "/jobs/". Matching them needs looser hints than matching a posting URL.
_JOB_SITEMAP_HINTS = ("job", "career", "vacanc", "position", "opening", "hiring")


# --- JSON-LD ---------------------------------------------------------------


def _walk(node: Any):
    """Yield every dict nested anywhere inside a decoded JSON-LD document."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def _is_job_posting(node: dict) -> bool:
    types = node.get("@type")
    if isinstance(types, str):
        types = [types]
    if not isinstance(types, list):
        return False
    return any(str(t).lower() == "jobposting" for t in types)


def _jsonld_identifier(node: dict) -> Any:
    """schema.org identifier is a string, a number, or a PropertyValue dict."""
    for key in ("identifier", "jobPostingId", "@id"):
        value = node.get(key)
        if isinstance(value, dict):
            value = value.get("value") or value.get("name")
        if value not in (None, "", []):
            return value
    return None


def _jsonld_location(node: dict) -> str | None:
    """Flatten jobLocation (which may be a list) into one raw display string."""
    places = node.get("jobLocation")
    if places is None:
        places = node.get("address")
    if isinstance(places, dict):
        places = [places]
    if not isinstance(places, list):
        places = []

    labels: list[str] = []
    for place in places:
        if isinstance(place, str):
            labels.append(place)
            continue
        if not isinstance(place, dict):
            continue
        address = place.get("address") if isinstance(place.get("address"), dict) else place
        labels.append(
            join_parts(
                [
                    address.get("addressLocality"),
                    address.get("addressRegion"),
                    address.get("addressCountry")
                    if not isinstance(address.get("addressCountry"), dict)
                    else address["addressCountry"].get("name"),
                ]
            )
            or place.get("name")
        )

    location = join_parts(labels, sep=" | ")
    if location:
        return location

    # Fully-remote postings often carry no jobLocation at all.
    remote = node.get("jobLocationType")
    if remote and "telecommute" in str(remote).lower():
        return "Remote"
    return None


async def jsonld_jobpostings(url: str, *, save_raw: bool = True) -> list[dict]:
    """Extract schema.org JobPosting entries from a page's ld+json blocks.

    Handles the three shapes seen in the wild: a bare JobPosting object, an
    array of them, and an @graph wrapper. A block that fails to parse is
    skipped — one malformed script tag must not lose the others on the page.
    """
    res = await polite_get(url, want_json=False)
    if not res.ok or not res.text:
        return []

    postings: list[dict] = []
    raw_blocks: list[Any] = []

    for block in _LDJSON_BLOCK.findall(res.text):
        text = block.strip()
        if not text:
            continue
        try:
            decoded = json.loads(text)
        except ValueError:
            # Some sites emit HTML-escaped or trailing-comma JSON. One bad
            # block is not worth failing the page over.
            continue
        raw_blocks.append(decoded)
        for node in _walk(decoded):
            if _is_job_posting(node):
                postings.append(node)

    if not postings:
        return []

    if save_raw:
        host = urlsplit(url).netloc or "page"
        save_fixture("custom_jsonld", host, {"source_url": url, "blocks": raw_blocks})

    jobs = []
    for node in postings:
        hiring = node.get("hiringOrganization")
        department = None
        if isinstance(node.get("employmentUnit"), dict):
            department = node["employmentUnit"].get("name")
        department = department or node.get("occupationalCategory") or node.get("industry")
        if isinstance(department, list):
            department = department[0] if department else None
        if isinstance(hiring, dict) and department is None:
            department = hiring.get("department")

        posting_url = node.get("url") or node.get("sameAs") or url
        if isinstance(posting_url, str) and posting_url.startswith("/"):
            posting_url = urljoin(url, posting_url)

        jobs.append(
            make_job(
                native_id=_jsonld_identifier(node),
                title=node.get("title") or node.get("name"),
                location=_jsonld_location(node),
                url=posting_url,
                posted_at=node.get("datePosted") or node.get("dateCreated"),
                department=department,
            )
        )
    return jobs


# --- sitemap ---------------------------------------------------------------


def _looks_like_job_url(loc: str) -> bool:
    """True when the path has a jobs-ish segment followed by at least one more.

    The trailing-segment requirement drops the board's own index page
    (/careers) while keeping every posting under it. It does still admit the
    occasional non-posting that lives under /careers/ — a generic reader has no
    way to tell /careers/our-process from /careers/staff-engineer, and erring
    toward inclusion is the right trade here.
    """
    segments = [s.lower() for s in urlsplit(loc).path.split("/") if s]
    return any(_JOB_SEGMENT.match(s) for s in segments[:-1])


def _looks_like_job_sitemap(loc: str) -> bool:
    lowered = loc.lower()
    return any(hint in lowered for hint in _JOB_SITEMAP_HINTS)


def _title_from_url(loc: str) -> str | None:
    """Derive a human-ish title from the last meaningful path segment.

    A slug is a poor title, but it is the only title a sitemap carries, and a
    weak title beats dropping the row. Trailing numeric ids are stripped.
    """
    path = urlsplit(loc).path.rstrip("/")
    if not path:
        return None
    segment = path.rsplit("/", 1)[-1]
    segment = re.sub(r"\.(html?|php|aspx)$", "", segment, flags=re.IGNORECASE)
    words = [w for w in re.split(r"[-_+]+", segment) if w and not w.isdigit()]
    if not words:
        return None
    return " ".join(w.capitalize() if w.islower() else w for w in words)


def _native_id_from_url(loc: str) -> str | None:
    """Prefer a numeric/uuid-looking path segment as the vendor id."""
    for segment in reversed([s for s in urlsplit(loc).path.split("/") if s]):
        if re.fullmatch(r"[0-9]{3,}", segment) or re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", segment, re.IGNORECASE
        ):
            return segment
    return None


async def sitemap_jobs(sitemap_url: str, *, save_raw: bool = True, _depth: int = 0) -> list[dict]:
    """Turn a careers sitemap.xml into one row per job URL.

    A <sitemapindex> is followed one level down, and only into child sitemaps
    whose own URL looks job-related, so we do not walk an entire marketing site.
    Rows carry a slug-derived title and lastmod as posted_at; location and
    department are simply unknown from a sitemap and stay None.
    """
    res = await polite_get(sitemap_url, want_json=False)
    if not res.ok or not res.text:
        return []
    body = res.text

    if _SITEMAPINDEX.search(body) and _depth == 0:
        children = [c.strip() for c in _LOC_TAG.findall(body) if _looks_like_job_sitemap(c)]
        collected: list[dict] = []
        for child in children[:10]:
            collected.extend(await sitemap_jobs(child, save_raw=save_raw, _depth=1))
        return collected

    entries: list[tuple[str, str | None]] = []
    for chunk in _URL_ENTRY.findall(body):
        loc_match = _LOC_TAG.search(chunk)
        if not loc_match:
            continue
        lastmod_match = _LASTMOD_TAG.search(chunk)
        entries.append((loc_match.group(1).strip(), lastmod_match.group(1).strip() if lastmod_match else None))

    if not entries:  # sitemap with bare <loc> tags and no <url> wrappers
        entries = [(loc.strip(), None) for loc in _LOC_TAG.findall(body)]

    rows = [(loc, lastmod) for loc, lastmod in entries if loc and _looks_like_job_url(loc)]
    if not rows:
        return []

    if save_raw:
        host = urlsplit(sitemap_url).netloc or "sitemap"
        save_fixture(
            "custom_sitemap",
            host,
            {"sitemap_url": sitemap_url, "entries": [{"loc": l, "lastmod": m} for l, m in rows]},
        )

    return [
        make_job(
            native_id=_native_id_from_url(loc),
            title=_title_from_url(loc),
            location=None,
            url=loc,
            posted_at=lastmod,
            department=None,
        )
        for loc, lastmod in rows
    ]
