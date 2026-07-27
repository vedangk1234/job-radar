r"""The second-stage filter: read the description, keep two facts, discard the text.

The title filter in match.py is cheap and blind. It reads a string a recruiter
wrote to be searchable, and a string like "Full Stack Builder" says nothing at
all about whether the role is engineering or product, or whether it wants two
years or eight. Both of those are stated plainly in the description, and both
are the difference between a digest worth opening and one worth ignoring.

So this module reads descriptions — and stores none of them.

    fetch_description   network -> plain text, in memory only
    detect_min_experience   text -> int | None
    detect_pm_role          text -> bool
    enrich_candidates       the five columns, written once per job, forever

Why never store the text
------------------------
crawler/store.py's docstring makes a promise: the jobs table holds metadata and
never a job description. That promise is what keeps this project on a free
Postgres tier. A description is 5-10 KB of HTML; a few thousand open jobs is
tens of megabytes of text nobody will ever read again, to answer two questions
that fit in an integer and a boolean.

The discipline is therefore structural rather than a habit: there is no column
to write text into, `enrich_candidates` hands the store five scalars, and the
string goes out of scope when the loop iteration ends.

Where this runs, and why it moved
---------------------------------
It runs in the CRAWL now (crawler/enrich.py), and it used to run only in the
digest path. The move closes a real hole rather than tidying anything.

The old arrangement enriched a job the first time a DIGEST considered it, which
meant the first time it was in the notified=FALSE queue. The dashboard's Home
screen is not that queue — it shows everything open that fits, emailed or not —
so a job the crawler had just discovered appeared on Home with desc_checked =
FALSE, no experience known, and therefore kept. A real Adobe posting
("Computer Scientist - Fullstack", 4-6 years) sat there for exactly that reason.

What did NOT change is the cost argument. The crawler sees the whole board —
788 open rows here — and fetching a description for each would be a second
request per job per crawl forever. So the pass still runs only over jobs that
already passed every TITLE test (36 of those 788), and still reads each one's
description exactly once. What changed is when that once happens: within one
crawl cycle of the job appearing, rather than whenever an email next goes out.

Why a failed fetch is now a different fact from a quiet one
-----------------------------------------------------------
`desc_checked` used to carry two meanings: "we read it and it said nothing
disqualifying" and "we asked and got nothing back". Both leave min_experience
NULL and both mean KEEP, so conflating them looked harmless — and it was not.
It meant a board with no description route was marked read-and-clean the first
time it was considered, permanently, on evidence that was never gathered. Every
Workday candidate in the table was in that state, because until this change
fetch_description had no Workday route at all.

So a failure now sets desc_fetch_failed as well, which lets crawler/enrich.py
ask again tomorrow (desc_checked_at is the "not more often than that" half)
without re-reading the descriptions that were genuinely read.

The inclusion bias, again
-------------------------
Both detectors are deliberately hard to convince, because their failures are not
symmetric. A missed PM role costs one line in one digest. A false positive
deletes a real job from the inbox and nothing downstream reports it. So:

  * a fetch that fails returns None, and None means KEEP;
  * a number is read as an experience requirement only when experience language
    is attached to it;
  * "product manager" in a body of text is a colleague until the sentence says
    the role IS one.
"""

from __future__ import annotations

import html
import json
import re
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

from fetchers.http import polite_get
from fetchers.workday import PER_HOST_DELAY as WORKDAY_DELAY

from .store import save_description_facts

# --- HTML -> plain text ---------------------------------------------------

_SCRIPT_RE = re.compile(r"(?is)<(script|style)\b.*?</\1\s*>")
# Tags that end a line of prose. Turning them into newlines rather than spaces
# keeps list items apart, which matters: "…3 years" </li><li> "experience with…"
# would otherwise fuse into a sentence that reads like a requirement it is not.
_BLOCK_RE = re.compile(r"(?i)<\s*(?:br|/p|/div|/li|/tr|/h[1-6]|/ul|/ol)\s*/?>")
_TAG_RE = re.compile(r"<[^>]+>")
_SPACES_RE = re.compile(r"[ \t ]+")
# `</p><p>` leaves a newline immediately followed by the space the opening tag
# became. Trimming both sides of every newline keeps the result a document
# instead of a document with a ragged left edge.
_EDGE_RE = re.compile(r" *\n *")
_BLANKS_RE = re.compile(r"\n\s*\n+")


def html_to_text(raw: Any) -> str | None:
    """Vendor description markup -> plain text, or None if there was nothing.

    Handles the Greenhouse quirk directly: `content` arrives HTML-ESCAPED, so
    the body is literally `&lt;div&gt;&lt;p&gt;…`, with not a single real angle
    bracket in it. Unescaping that first is what turns it into markup this can
    then strip. The pre-unescape is guarded on "no real tags present" so a
    normal HTML description — where `&lt;` is a literal angle bracket the
    recruiter typed — is never mangled into a tag and deleted.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None

    text = raw
    if "<" not in text and "&lt;" in text:
        text = html.unescape(text)

    text = _SCRIPT_RE.sub(" ", text)
    text = _BLOCK_RE.sub("\n", text)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)  # &amp; and friends that sat inside the prose

    text = _SPACES_RE.sub(" ", text)
    text = _EDGE_RE.sub("\n", text)
    text = _BLANKS_RE.sub("\n", text)
    return text.strip() or None


def _joined(parts: Iterable[Any]) -> str | None:
    """Concatenate the non-empty pieces of a description into one document."""
    kept = [str(p).strip() for p in parts if isinstance(p, str) and p.strip()]
    return "\n".join(kept) or None


# --- minimum years of experience ------------------------------------------
#
# This is the dangerous detector, and the danger is one-directional. Company
# boilerplate is FULL of numbers, and every one of them is a number about
# something else:
#
#     "processes $180 billion in annualized transactions"
#     "you'll be part of a 3000+ strong team"
#     "Founded in 2014 by Harshil Mathur"
#     "having raised over $740 million to date"
#     "over 100 of India's unicorns"
#
# Every one of those sits in the same Razorpay description as the single line
# that actually states a requirement ("3-6 years building software as technical
# PM"). A detector that reads bare numbers would find five requirements in that
# text, pick one at random, and drop or keep the job for a reason that has
# nothing to do with the job.
#
# So nothing is read as experience unless a duration unit (years/yrs) is
# attached to it AND experience language sits next to that. Money, headcounts,
# founding dates and customer counts carry none of those, and are invisible here.

# The words that make a duration a REQUIREMENT rather than a duration.
_CONTEXT = r"(?:experience|\bexp\b|building|working|relevant|of[ \t]+work)"
_CONTEXT_RE = re.compile(_CONTEXT, re.IGNORECASE)

# "3-6 years", "2 to 4 yrs", "0–3 years", "4-6yrs". Two bounds, one unit, and the
# LOWER bound is the minimum — a range is a request for at least its floor.
_RANGE_RE = re.compile(
    r"(\d{1,2})\s*(?:[-–—]|to)\s*(\d{1,2})\s*\+?\s*(?:years?|yrs?)\b", re.IGNORECASE
)

# "5+ years experience", "3 years of relevant experience", "4 yrs building".
# The lazy {0,2} lets a couple of adjectives ("of hands-on") stand between the
# unit and the context word without letting the whole sentence in.
#
# Every gap here is [ \t] and never \s, so the match cannot cross a NEWLINE.
# That restriction is doing real work on real markup: "<li>2 years</li><li>
# experience with Kafka</li>" is two separate bullets, and reading across the
# break between them invents a requirement neither of them stated.
_YEARS_RE = re.compile(
    r"(\d{1,2})[ \t]*\+?[ \t]*(?:years?|yrs?)\b[ \t,]*"
    r"(?:of[ \t]+|in[ \t]+|with[ \t]+)?(?:[\w\-]+[ \t\-]+){0,2}?" + _CONTEXT,
    re.IGNORECASE,
)

# "minimum 3 years", "min. 2 yrs", "at least 2 years". The qualifier IS the
# experience context here, so no second anchor is required.
_MINIMUM_RE = re.compile(
    r"(?:minimum|min\.?|at\s*least|atleast)\s+(?:of\s+)?(\d{1,2})\s*\+?\s*(?:years?|yrs?)\b",
    re.IGNORECASE,
)

# Roles that say outright that they want no experience at all.
_ENTRY_RE = re.compile(
    r"\bfreshers?\b|\bentry[\s\-]?level\b|\bnew\s+grads?\b|\bnew\s+graduates?\b"
    r"|\brecent\s+graduates?\b|\bcampus\s+hire\b",
    re.IGNORECASE,
)

# Above this, a "year" figure is something other than a career: a headcount, a
# calendar year that slipped through, a typo. Nobody asks for 41 years.
_MAX_PLAUSIBLE_YEARS = 40


def _line_has_context(text: str, start: int, end: int) -> bool:
    """Is there experience language on the SAME LINE as the match?

    The line, not a character window. A first attempt used a 40-character window
    and it silently failed on a real PhonePe posting: "5–9 years of full-time iOS
    app development experience" put the anchoring word 42 characters away, the
    window cut it in half, and a role wanting five to nine years read as stating
    no requirement at all. Counting characters was measuring the wrong thing.

    The line boundary is the right unit because a requirement is written inside
    one bullet, and it is a real boundary: a benefits paragraph below cannot lend
    the word "experience" to a duration in the bullet above.

    Only the two-bound RANGE form is allowed this much room. `N-M years` is a
    shape prose does not produce by accident, whereas a single "N years" is
    everywhere ("in the 3 years since we were founded…"), so _YEARS_RE stays
    tight — two filler words, never crossing a newline.
    """
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    before = text[line_start:start]
    after = text[end:] if line_end == -1 else text[end:line_end]
    return bool(_CONTEXT_RE.search(before) or _CONTEXT_RE.search(after))


def detect_min_experience(text: Any) -> int | None:
    """The smallest minimum the description confidently asks for, or None.

    None means "nothing experience-anchored was found", which is the answer for
    the overwhelming majority of descriptions and always means KEEP. Silence is
    not a requirement of zero and not a requirement of ten; it is silence.

    The SMALLEST confident figure wins when a description states several. A JD
    that says "2+ years required, 5+ preferred" is a 2-year job, and reading the
    larger number would drop exactly the roles worth applying to.
    """
    if not isinstance(text, str) or not text.strip():
        return None

    found: list[int] = []

    for match in _RANGE_RE.finditer(text):
        low, high = int(match.group(1)), int(match.group(2))
        if low > high or high > _MAX_PLAUSIBLE_YEARS:
            continue
        # A range that starts at zero states an entry-level opening outright;
        # it needs no second anchor, and the answer it produces (0) is the one
        # answer that can never cost us a job.
        if low == 0 or _line_has_context(text, *match.span()):
            found.append(low)

    for pattern in (_YEARS_RE, _MINIMUM_RE):
        for match in pattern.finditer(text):
            years = int(match.group(1))
            if years <= _MAX_PLAUSIBLE_YEARS:
                found.append(years)

    if _ENTRY_RE.search(text):
        found.append(0)

    return min(found) if found else None


# --- product / people management ------------------------------------------
#
# The failure to design against is the ordinary engineering JD. Almost every one
# of them mentions product managers, because almost every engineer works with
# one: "partner with product managers and designers", "you'll report to the
# engineering manager", "collaborate closely with product". None of those makes
# the posting a PM role, and a detector that fired on the bare phrase would
# delete most of the backend jobs in the digest.
#
# So a management term counts only when the sentence points it AT THE ROLE —
# "as a product manager", "we're hiring a program manager", "you will be the
# product owner" — or when the role's own title says so.

_PM_TERM = (
    r"(?:(?:technical|senior|associate|lead|group|principal|staff)\s+)?"
    r"(?:product\s+manager|product\s+management|program(?:me)?\s+manager|"
    r"product\s+owner|people\s+manager|engineering\s+manager|technical\s+pm)"
)

# In a TITLE the term needs no framing: a posting called "Engineering Manager"
# is not collaborating with one. (The 'manager' word exclusion in match.py
# already drops most of these at the title stage; this is the backstop for the
# ones spelled differently, and it makes detect_pm_role honest when called on
# its own.)
_PM_TITLE_RE = re.compile(r"\b" + _PM_TERM + r"\b", re.IGNORECASE)

# In a BODY it needs framing. Every alternative below is a phrase that can only
# introduce the role being advertised — never a colleague it works alongside.
_PM_FRAMED_RE = re.compile(
    r"\b(?:as\s+an?|as|is\s+an?|be\s+an?|become\s+an?|hiring\s+an?|seeking\s+an?|"
    r"looking\s+for\s+an?|position\s+of\s+an?|this\s+is\s+an?|"
    r"you(?:'ll|\s+will)\s+be\s+(?:an?|the)?)\s*" + _PM_TERM + r"\b",
    re.IGNORECASE,
)

# Managing people is management whatever the title calls it.
_MANAGES_PEOPLE_RE = re.compile(
    r"\bmanag(?:e|es|ing)\s+(?:a\s+)?team\s+of\b|\bdirect\s+reports\b", re.IGNORECASE
)


def detect_pm_role(title: Any, text: Any) -> bool:
    """Is this posting fundamentally a product/people management job?

    True on strong signals only — the title naming a management role, the body
    framing the role itself as one ("3-6 years building software as technical
    PM", "describe how you leverage AI in your work as a Product Manager"), or
    the body handing it a team to manage.

    False for the engineering role that merely works with product managers,
    which is nearly all of them.
    """
    if isinstance(title, str) and _PM_TITLE_RE.search(title):
        return True
    if not isinstance(text, str) or not text.strip():
        return False
    return bool(_PM_FRAMED_RE.search(text) or _MANAGES_PEOPLE_RE.search(text))


# --- fetching the description ---------------------------------------------
#
# Two shapes of source, and the difference is one request versus zero.
#
# Lever, Ashby, Workable (details=true) and Recruitee all ship the description
# inside the same list payload the crawler already knows how to ask for, so the
# whole board is fetched once and every candidate on it is answered from memory.
#
# Greenhouse's list endpoint deliberately does NOT carry content — fetchers/
# tier1.py leaves ?content=true off precisely because it inflates every row with
# megabytes we never store. The single-job endpoint carries it instead, so
# Greenhouse costs one request per candidate. Verified live against
# boards-api.greenhouse.io/v1/boards/{token}/jobs/{id}?content=true, which
# answers 200 with an HTML-escaped `content` field.

GREENHOUSE_JOB_URL = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{job_id}"

# Workday's own detail route, and the reason this file exists in its current
# form. The board search endpoint the crawler pages through carries no
# description at all — but the CXS host serves one per posting at the same path
# the public job URL uses, minus the language segment:
#
#   public : https://{host}/en-US/{site}/job/Bangalore/Computer-Scientist_R168336
#   detail : https://{host}/wday/cxs/{tenant}/{site}/job/Bangalore/Computer-Scientist_R168336
#            -> {"jobPostingInfo": {"jobDescription": "<div>…", …}, …}
#
# Verified live against the Adobe tenant: 200, ~14 KB of HTML, and the line
# "4-6 years of experience in software engineering." inside it — which is the
# posting this whole change exists to stop showing.
WORKDAY_JOB_URL = "https://{host}/wday/cxs/{tenant}/{site}{path}"

# The list endpoints, identical to the ones in fetchers/tier1.py. Duplicated as
# a mapping rather than imported because tier1 returns NORMALISED rows, which by
# design have already dropped the description; what is needed here is the raw row.
_BOARD_URLS = {
    "lever": "https://api.lever.co/v0/postings/{token}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{token}",
    "workable": "https://apply.workable.com/api/v1/widget/accounts/{token}?details=true",
    "recruitee": "https://{token}.recruitee.com/api/offers/",
}
_BOARD_ROOT_KEY = {"lever": None, "ashby": "jobs", "workable": "jobs", "recruitee": "offers"}

# The row fields that can carry the vendor's own id, in the same order the
# matching fetcher in tier1.py reads them — so the key built here is the same
# string that ended up inside canonical_id.
_BOARD_ID_FIELDS = {
    "lever": ("id",),
    "ashby": ("id",),
    "workable": ("shortcode", "id"),
    "recruitee": ("id", "guid"),
}

# One board fetch per (ats, token) per process. A digest run asks about a
# handful of jobs and they cluster by company; without this, three Meesho
# candidates would mean three identical downloads of the same Lever board.
_board_cache: dict[tuple[str, str], dict[str, dict]] = {}


def reset_board_cache() -> None:
    """Forget every cached board. For tests and for long-lived processes."""
    _board_cache.clear()


def native_id_from(canonical_id: Any) -> str | None:
    """`greenhouse:12:4712960005` -> `4712960005`; a hashed id -> None.

    canonical_id is `{ats}:{company_id}:{native_id}` (fetchers/normalize.py), so
    the vendor's own id is recoverable without a second column. Boards that
    published no id got a `hash:` id instead, and there is nothing to ask a
    vendor API about — those return None and are kept.
    """
    text = str(canonical_id or "").strip()
    if not text or text.startswith("hash:"):
        return None
    parts = text.split(":", 2)
    if len(parts) != 3 or not parts[2].strip():
        return None
    return parts[2].strip()


def description_from_row(ats: str, row: Mapping[str, Any]) -> str | None:
    """One raw vendor row -> its description as plain text, or None.

    Each ATS splits a description across different fields, and missing one of
    them is not a cosmetic loss: Lever keeps the intro in `description` and the
    REQUIREMENTS — where every years-of-experience line lives — in `lists`.
    Reading only the intro would make every Lever posting look like it stated no
    experience at all.
    """
    if ats == "lever":
        parts: list[Any] = [row.get("descriptionPlain") or html_to_text(row.get("description"))]
        for section in row.get("lists") or []:
            if isinstance(section, dict):
                parts.append(section.get("text"))
                parts.append(html_to_text(section.get("content")))
        parts.append(row.get("additionalPlain") or html_to_text(row.get("additional")))
        return _joined(parts)

    if ats == "ashby":
        plain = row.get("descriptionPlain")
        if isinstance(plain, str) and plain.strip():
            return plain.strip()
        return html_to_text(row.get("descriptionHtml"))

    if ats == "workable":
        return _joined(
            [
                html_to_text(row.get("description")),
                html_to_text(row.get("requirements")),
                html_to_text(row.get("benefits")),
            ]
        )

    if ats == "recruitee":
        return _joined([html_to_text(row.get("description")), html_to_text(row.get("requirements"))])

    return None


def _rows_of(payload: Any, key: str | None) -> list[dict]:
    if key is None:
        rows = payload if isinstance(payload, list) else []
    else:
        rows = payload.get(key) if isinstance(payload, dict) else []
    return [r for r in rows or [] if isinstance(r, dict)]


async def _board_index(ats: str, token: str) -> dict[str, dict]:
    """{native_id: raw row} for one board, fetched at most once per process."""
    cache_key = (ats, token)
    if cache_key in _board_cache:
        return _board_cache[cache_key]

    res = await polite_get(_BOARD_URLS[ats].format(token=token))
    index: dict[str, dict] = {}
    if res.ok:
        for row in _rows_of(res.json, _BOARD_ROOT_KEY[ats]):
            for field in _BOARD_ID_FIELDS[ats]:
                value = row.get(field)
                if value is not None and str(value).strip():
                    index.setdefault(str(value).strip(), row)
                    break

    # Cached even when empty: a board that 404s or returns nothing will do the
    # same thing on the next candidate, and re-asking would be rude, not braver.
    _board_cache[cache_key] = index
    return index


async def _greenhouse_description(token: str, job_id: str) -> str | None:
    """The single-job endpoint, which is the only Greenhouse route with content.

    `questions=true` comes along because the application form is part of how a
    posting states what it is: the Razorpay "Full Stack Builder" asks applicants
    to "describe how you leverage AI in your work as a Product Manager", which
    is the role telling you what it is more plainly than its title does.
    """
    res = await polite_get(
        GREENHOUSE_JOB_URL.format(token=token, job_id=job_id),
        params={"content": "true", "questions": "true"},
    )
    if not res.ok or not isinstance(res.json, dict):
        return None

    parts = [html_to_text(res.json.get("content"))]
    for question in res.json.get("questions") or []:
        if isinstance(question, dict):
            parts.append(question.get("label"))
    return _joined(parts)


def workday_external_path(site: str, url: Any) -> str | None:
    """`https://h/en-US/mysite/job/Bangalore/X_R1` -> `/job/Bangalore/X_R1`.

    Recovered from the stored URL rather than kept in a column, because the URL
    is what fetchers.workday.build_url already assembled out of the externalPath
    and it is the only piece of the posting we persist. The site segment is the
    landmark: everything to the right of it is the path the CXS route wants, and
    the language segment to its left is exactly what that route must not carry.

    None when the URL is not of that shape — an unknown shape is not something
    to guess at, and "we could not build a request" is a fetch failure, which is
    a state this module knows how to hold.
    """
    site = str(site or "").strip().strip("/")
    path = urlsplit(str(url or "")).path
    if not site or not path:
        return None
    marker = f"/{site}/"
    index = path.find(marker)
    if index == -1:
        return None
    return path[index + len(site) + 1 :]


async def _workday_description(company: Mapping[str, Any], job: Mapping[str, Any]) -> str | None:
    """One Workday posting's description, from the tenant's own CXS detail route.

    Paced at Workday's gentler 2 s/host rather than the global 1 s — the same
    number fetchers/workday.py uses for the board itself, imported rather than
    repeated so a future decision to slow down only has to be taken once. It is
    the same shared client, semaphore and backoff as every other fetch here.
    """
    config = config_of(company)
    host = str(config.get("host") or "").strip().rstrip("/")
    site = str(config.get("site") or "").strip().strip("/")
    path = workday_external_path(site, job.get("url"))
    tenant = str(company.get("token") or "").strip()
    if not host or not site or not path or not tenant:
        return None

    res = await polite_get(
        WORKDAY_JOB_URL.format(host=host, tenant=tenant, site=site, path=path),
        min_delay=WORKDAY_DELAY,
    )
    if not res.ok or not isinstance(res.json, dict):
        return None

    info = res.json.get("jobPostingInfo")
    if not isinstance(info, dict):
        return None
    return html_to_text(info.get("jobDescription"))


async def fetch_description(
    company: Mapping[str, Any], job: Mapping[str, Any]
) -> str | None:
    """This job's full description as plain text, or None.

    None is not an error condition to handle upstream — it is the answer "we do
    not know", and every caller treats not knowing as KEEP. A dead board token,
    an ATS with no public description route, a posting that vanished between the
    crawl and the digest: all of them mean one extra line in one email, which is
    the correct price for never silently deleting a real job.

    Politeness is the shared kind: this goes through fetchers.http.polite_get,
    so the global concurrency cap, the per-host 1 req/sec pace and the 429/5xx
    backoff are the same ones the crawler runs under.
    """
    ats = str(company.get("ats") or "").strip().lower()
    token = str(company.get("token") or "").strip()
    if not ats or not token:
        return None

    native_id = native_id_from(job.get("canonical_id")) or job.get("native_id")

    if ats == "greenhouse":
        return await _greenhouse_description(token, str(native_id)) if native_id else None

    if ats == "workday":
        return await _workday_description(company, job)

    if ats in _BOARD_URLS:
        if not native_id:
            return None
        row = (await _board_index(ats, token)).get(str(native_id))
        return description_from_row(ats, row) if row else None

    # smartrecruiters and the custom scrapers publish no description route this
    # module has verified. Unknown, therefore kept — and now recorded as a
    # FAILED read rather than a clean one, so it is asked about again tomorrow
    # instead of being believed forever.
    return None


# --- the enrichment pass ---------------------------------------------------


def company_of(job: Mapping[str, Any]) -> dict[str, Any]:
    """The company half of a candidate row, as the value fetch_description wants."""
    return {"ats": job.get("ats"), "token": job.get("token"), "config": job.get("config")}


def config_of(company: Mapping[str, Any]) -> dict[str, Any]:
    """companies.config as a dict, whatever the driver handed us.

    asyncpg returns JSONB as a string unless a codec is registered, and the
    SQLite shim stores it as text — the same accommodation crawler.loop.Company
    already makes. Workday is the first ATS here whose request cannot be built
    without reading it, so the decode had to stop being someone else's problem.
    """
    config = company.get("config")
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except ValueError:
            return {}
    return config if isinstance(config, dict) else {}


def as_bool(value: Any) -> bool:
    """asyncpg hands back a real bool; the SQLite shim hands back 0/1."""
    return bool(value)


def _as_datetime(value: Any) -> datetime | None:
    """A stored timestamp as an aware datetime, or None if it is not one.

    asyncpg gives a real datetime; the SQLite shim gives the ISO string it wrote.
    A naive value is read as UTC, because that is what both sides store.
    """
    if isinstance(value, datetime):
        stamp = value
    elif isinstance(value, str) and value.strip():
        try:
            stamp = datetime.fromisoformat(value.strip())
        except ValueError:
            return None
    else:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def needs_description_check(job: Mapping[str, Any], *, retry_before: datetime | None = None) -> bool:
    """Is this job's description worth a request right now?

    Never read     -> yes, always. This is the ordinary case and the only one
                      the digest path has ever used.
    Read cleanly   -> no, ever. The facts are on the row; the text is gone and
                      re-deriving it would cost a request to learn nothing.
    Read and FAILED-> only if that failure is older than `retry_before`.

    `retry_before` is None for callers that do not want retries at all, which
    makes the third case behave exactly like the second. crawler/enrich.py is
    the one caller that passes a cutoff: a dead board is worth one more question
    tomorrow, and worth none at all thirty minutes from now.

    A failed row with no timestamp is retried once — that is a row the old
    schema wrote, before there was a column to record when we looked.
    """
    if not as_bool(job.get("desc_checked")):
        return True
    if retry_before is None or not as_bool(job.get("desc_fetch_failed")):
        return False
    checked_at = _as_datetime(job.get("desc_checked_at"))
    return checked_at is None or checked_at < retry_before


async def enrich_candidates(
    conn,
    candidates: Iterable[Mapping[str, Any]],
    *,
    fetcher=None,
    retry_before: datetime | None = None,
) -> list[dict[str, Any]]:
    """Read the description of every candidate that needs one; store the facts.

    Returns one small dict per job it looked at — canonical_id, title, the two
    computed facts, and whether a description was actually retrieved — so a
    crawl or a preview run can explain itself. The description text is in none
    of them, and is unreachable the moment the loop moves on.

    Idempotent by construction: needs_description_check gates the whole loop, so
    a second run in the same cycle fetches nothing. A job whose description
    could not be fetched is still marked checked — retrying a dead endpoint
    every thirty minutes forever is not persistence, it is a loop — but it is
    marked desc_fetch_failed too, so `retry_before` can bring it back tomorrow.

    What this writes is deliberately five columns and no more: it must not be
    able to disturb `notified`, `status`, `miss_count` or `first_seen_at`. See
    SAVE_DESCRIPTION_FACTS_SQL, which has no clause that could.

    Sequential rather than gathered, on purpose. The set is a handful by the
    time it gets here, the shared client already paces itself per host, and one
    request at a time is the version whose behaviour against someone else's
    board is obvious from reading it.
    """
    fetch = fetcher or fetch_description
    report: list[dict[str, Any]] = []
    facts: list[tuple[str, int | None, bool, bool]] = []

    for job in candidates:
        if not needs_description_check(job, retry_before=retry_before):
            continue

        text = await fetch(company_of(job), job)
        failed = text is None
        min_experience = detect_min_experience(text)
        is_pm_role = detect_pm_role(job.get("title"), text)

        # The row dict is updated in place so the caller's very next is_match()
        # sees these facts without a re-read of the table.
        job["min_experience"] = min_experience  # type: ignore[index]
        job["is_pm_role"] = is_pm_role  # type: ignore[index]
        job["desc_checked"] = True  # type: ignore[index]
        job["desc_fetch_failed"] = failed  # type: ignore[index]

        facts.append((job["canonical_id"], min_experience, is_pm_role, failed))
        report.append(
            {
                "canonical_id": job["canonical_id"],
                "title": job.get("title"),
                "company": job.get("company"),
                "min_experience": min_experience,
                "is_pm_role": is_pm_role,
                "fetched": not failed,
            }
        )
        # `text` is rebound on the next iteration and never leaves this scope.

    await save_description_facts(conn, facts)
    return report
