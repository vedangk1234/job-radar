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
meant the first time it reached somebody's send queue. The dashboard's Home
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
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, NamedTuple
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

# A duration that OPENS ITS OWN LINE is a requirement, whatever else the line
# says. This is the third anchor, and it exists because the other two both
# missed a real InMobi posting — "SDE 2", Lucknow — whose Required Skills
# section reads:
#
#     3–6 years in data/platform engineering
#     Experience with Airflow (or similar orchestration tools)
#
# _RANGE_RE finds "3–6 years" and then throws it away, because _line_has_context
# looks for experience language on that line and the line says "engineering"
# instead. _YEARS_RE cannot reach the word "experience" on the NEXT line, by
# design — reading across a bullet break is how a requirement gets invented. So
# a bullet that states its requirement in the ordinary way, without ever using
# the word, stated nothing at all. The sibling posting "SDE II - Salesforce AI"
# leaked the same way on "4+ years of Salesforce development (Lightning
# Platform)".
#
# The position IS the context. html_to_text turns every </li> into a newline, so
# a line beginning "3–6 years …" or "4+ years …" is a bullet in a requirements
# list, and prose does not begin a paragraph that way. That makes this a
# stricter test than it looks: only the leading token counts, so the
# "3000+ strong team" and "over 100 unicorns" numbers this module was built to
# ignore stay invisible — they are mid-sentence, and they are not years.
#
# Two guards keep it honest. The lower bound of a range is what is captured (a
# range asks for at least its floor), and the nouns below are excluded outright
# — the shapes that can genuinely lead a line and still not be a career:
#
#     "10 years ago we started in a garage"     retrospective prose
#     "5 years of service award"                a benefits bullet
#     "3 years of growth"                       an about-us bullet
#
# The last two are the reason this is a lookahead and not just a position test.
# A perks list is bulleted exactly like a requirements list, so position alone
# would read "5 years of service" as a five-year minimum and delete the job —
# the silent, one-directional failure the whole module is arranged against.
_NOT_A_CAREER = (
    r"(?:ago|since|back|"
    r"(?:of[ \t]+|in[ \t]+)?"
    r"(?:service|vesting|warranty|operation|growth|history|business|tenure)\b)"
)

_LEADING_DURATION_RE = re.compile(
    r"^[ \t]*[-–—•*●▪]?[ \t]*"
    r"(\d{1,2})[ \t]*(?:(?:[-–—]|to)[ \t]*\d{1,2}[ \t]*)?\+?[ \t]*"
    r"(?:years?|yrs?)\b"
    r"(?![ \t]*" + _NOT_A_CAREER + r")",
    re.IGNORECASE | re.MULTILINE,
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

    Three anchors make a number readable as a requirement, and a duration needs
    only one of them: experience LANGUAGE beside it (_YEARS_RE, _MINIMUM_RE, and
    the same-line test _RANGE_RE uses), or POSITION at the head of its own
    bullet (_LEADING_DURATION_RE). The second was added because a requirements
    list that never says the word "experience" is completely ordinary, and used
    to read as stating no requirement at all.

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

    for pattern in (_YEARS_RE, _MINIMUM_RE, _LEADING_DURATION_RE):
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


# --- work arrangement: onsite / hybrid / remote ----------------------------
#
# TWO TIERS, AND THE FIRST ONE IS NOT A PARSER AT ALL.
#
# Three of the five live ATSes publish the answer as a field, so guessing it out
# of prose there would be inventing a verdict next to a stated one. Verified live
# against real boards rather than taken from documentation, which is the same
# discipline the Workday country facet earned the hard way:
#
#   ashby            workplaceType  "OnSite" | "Hybrid" | "Remote"   (also isRemote)
#   lever            workplaceType  "onsite" | "hybrid" | "remote"
#   smartrecruiters  location.remote / location.hybrid, two booleans
#
# All three ride in a payload this module ALREADY fetches, so the structural tier
# costs zero extra requests.
#
# GREENHOUSE AND WORKDAY PUBLISH NOTHING. Greenhouse's board API carries
# `location.name`, `offices` and a free-form `metadata` array and no workplace
# field of any kind; Workday's CXS detail route carries `jobRequisitionLocation`,
# `timeType` and `location`, and again nothing. Both were checked against live
# postings whose location string says "Remote - US". That is 5,589 of 7,858 open
# rows, and it is why the prose tier below has to exist and has to be careful.
#
# THE BIAS IS THE MIRROR IMAGE OF detect_pm_role's, and getting that backwards is
# the whole risk here. A false PM verdict HIDES a job. A false remote verdict
# ADMITS one — notify.match.city_ok lets a remote posting skip the city gate — so
# `remote` is the hard-to-convince answer and `onsite` is the resting state.

WORK_MODES = ("onsite", "hybrid", "remote")

# What a row with nothing written on it is READ AS at match time. It is not
# written to the column: see notify.match.work_mode_of, and the module docstring
# of scripts/backfill_work_mode.py for why NULL is never backfilled.
DEFAULT_WORK_MODE = "onsite"


def work_mode_from_row(ats: Any, row: Mapping[str, Any] | None) -> str | None:
    """The ATS's own workplace field, or None if this vendor publishes none.

    Read, never inferred. A vendor that states the arrangement is a better
    authority on it than any regex over the prose underneath, and the three that
    do state it are the three whose description already arrives in a board
    payload — so this is free.

    smartrecruiters is two booleans rather than an enum, and both being FALSE is
    a real answer ("neither remote nor hybrid" is onsite) rather than a silence.
    Hybrid is tested first for the reason the prose tier gives below.
    """
    ats = str(ats or "").strip().lower()
    if not isinstance(row, Mapping):
        return None

    if ats in ("ashby", "lever"):
        value = str(row.get("workplaceType") or "").strip().lower()
        return value if value in WORK_MODES else None

    if ats == "smartrecruiters":
        location = row.get("location")
        if isinstance(location, Mapping):
            if location.get("hybrid") is True:
                return "hybrid"
            if location.get("remote") is True:
                return "remote"
            if location.get("remote") is False and location.get("hybrid") is False:
                return "onsite"

    return None


# THE PHRASES WHERE "remote" IS A TECHNICAL NOUN AND NOT AN ARRANGEMENT.
#
# This list is the reason the prose tier is safe to run at all. "Remote" is the
# one arrangement word with a heavy second life in engineering English, and every
# phrase below appears in ordinary Bangalore-onsite backend JDs:
#
#     "you will manage a remote team of eight engineers"
#     "debugging remote servers over SSH"
#     "experience with remote procedure calls"
#     "push to the remote repository"
#     "we support remote work for some teams"
#
# A detector that read any of those as "this posting is fully remote" would relax
# the city gate on a job that is nothing of the kind, and the failure is silent —
# the job simply turns up in a digest it should never have reached.
_REMOTE_NOT_AN_ARRANGEMENT_RE = re.compile(
    r"\bremote\s+(?:team|teams|colleague|colleagues|coworkers|co-workers|peers|"
    r"workforce|employees|staff|engineers|developers|members|"
    r"server|servers|host|hosts|machine|machines|desktop|device|devices|"
    r"system|systems|access|debugging|monitoring|sensing|execution|"
    r"procedure|repository|repositories|branch|branches|origin|"
    r"api|apis|endpoint|endpoints|call|calls|data|database|databases|"
    r"connection|connections|session|sessions|node|nodes|cluster|clusters|"
    r"config|configuration|state|cache|storage|"
    r"culture|work\s+culture|collaboration|communication)\b"
    r"|\bmanag(?:e|es|ing|ement\s+of)\s+(?:a\s+|an\s+)?(?:\w+\s+){0,2}remote\b"
    r"|\b(?:lead|leads|leading|mentor|mentors|mentoring)\s+(?:a\s+)?remote\b"
    r"|\bdistributed\s+team[s]?\b"
    r"|\bremote\s+procedure\s+call\b|\brpc\b"
    r"|\bsupport(?:s|ing)?\s+remote\s+work\b"
    r"|\bremote[- ]friendly\s+culture\b"
    r"|\bacross\s+remote\b|\bwith\s+remote\b"
    # A REMOTE WORK *ENVIRONMENT* IS A KIND OF WORKPLACE IN GENERAL, not this
    # workplace. "Experience in fully remote work environments" is a sentence
    # about the candidate's history, and it is a requirements bullet in postings
    # that are not remote at all.
    r"|\bremote\s+(?:work\s+)?(?:environment|environments|setting|settings|culture)\b"
    # THE CANDIDATE'S PAST, NOT THE ROLE'S PRESENT — and this one was found by the
    # dry run rather than by review, which is why the dry run exists.
    #
    # A Stripe posting, "Software Engineer, Secrets Infrastructure", location
    # field left as "na", requirements list reading "...Experience operating
    # complex services on AWS. Experience in fully remote work environments."
    # _REMOTE_FRAMED_RE's "fully remote" fired, the job was called remote, the
    # city gate relaxed, and a US posting became the ONE job this whole slice
    # would have added to the operator's digest. Precisely the failure the
    # framing discipline exists to prevent, arriving through a phrase that IS
    # framed — just pointed at the applicant instead of at the job.
    #
    # Bounded by [^.\n] so it cannot reach across a sentence: "Prior experience
    # is a plus. This role is fully remote." keeps its second sentence.
    r"|\b(?:experience|experienced|comfortable|familiar|thrive[sd]?|thriving|"
    r"worked|success|successful|proven|track\s+record)\b"
    r"[^.\n]{0,40}?\b(?:100\s*%\s*|fully\s+|entirely\s+|completely\s+)?remote\b",
    re.IGNORECASE,
)

# The same problem one word over, and less obvious. "Hybrid" is all over
# engineering prose that has nothing to do with where anybody sits — "hybrid
# cloud", "hybrid app", "hybrid model" in an ML JD, "hybrid search". Bare
# "hybrid" is therefore never enough; the framed patterns below are what count.
_HYBRID_NOT_AN_ARRANGEMENT_RE = re.compile(
    r"\bhybrid\s+(?:cloud|app|apps|application|applications|mobile|architecture|"
    r"approach|model|models|search|index|infrastructure|deployment|deployments|"
    r"network|networks|storage|encryption|solution|solutions|system|systems|"
    r"framework|stack|environment\s+of\s+services)\b",
    re.IGNORECASE,
)


def _mask(text: str, pattern: re.Pattern[str]) -> str:
    """Blank every match of `pattern`, same-length, so nothing else can read it.

    Exactly notify.match._mask_experience_numbers' idiom and for exactly its
    reason: the replacement is the same length, so every offset and every \\b in
    the patterns that run afterwards still lines up with the original text, and
    '#' is a non-word character so a masked phrase cannot be matched back out.

    Masking rather than "find a hit, then check whether it was a false one"
    because a description contains MANY mentions. "We debug remote servers. This
    role is fully remote." has to answer remote, and "You manage a remote team
    from our Bengaluru office" has to answer onsite — a whole-text veto would get
    the first wrong and a first-hit-wins scan would get the second wrong.
    """
    out = list(text)
    for match in pattern.finditer(text):
        out[match.start() : match.end()] = "#" * (match.end() - match.start())
    return "".join(out)


# HYBRID, FRAMED. Every alternative names the WORK rather than a technology.
_HYBRID_FRAMED_RE = re.compile(
    r"\bhybrid\s+(?:work(?:ing|place|\s+model|\s+week)?|role|position|job|schedule|"
    r"setup|set-up|arrangement|arrangements|policy|environment|office|mode)\b"
    r"|\b(?:role|position|job|this)\s+is\s+(?:a\s+)?hybrid\b"
    r"|\bwork(?:ing)?\s+model\s*[:\-]?\s*hybrid\b"
    r"|\bhybrid\s*[-–—(:]\s*\d+\s*days?\b"
    r"|\b\d+\s*days?\s+(?:a|per)\s+week\s+(?:in|from|at)\s+(?:the\s+)?office\b"
    r"|\b\d+\s*days?\s+(?:in|from|at)\s+(?:the\s+)?office\b"
    r"|\bin[- ]office\s+\d+\s*days?\b",
    re.IGNORECASE,
)

# REMOTE, FRAMED — AND DELIBERATELY STRICT. Every alternative is an unambiguous
# statement that THIS POSTING is remote. Weak and merely suggestive language does
# not appear here and does not earn the label.
#
# THE ASYMMETRY THAT DECIDES WHERE THE LINE SITS. A wrongly-ONSITE job is a quiet
# miss: it stays in whatever city bucket it was already in, and an account that
# ticked that city still sees it. A wrongly-REMOTE job BYPASSES THE CITY GATE —
# notify.match.city_ok exempts it — so it can arrive in a digest from anywhere in
# the country on the strength of one misread sentence. The two errors are not the
# same size, so the threshold is not in the middle.
#
# WHAT WAS CONSIDERED AND REJECTED, each for a stated reason rather than by
# omission:
#
#   remote-first, all-remote      statements about the COMPANY, not the posting.
#                                 "GitLab is an all-remote company" appears in
#                                 every GitLab JD including the office-bound ones.
#   remote-friendly               explicitly weak; it means "we tolerate it".
#   flexible, distributed         say nothing about where the job is.
#   wfh, work from home           reached the label in an earlier draft and both
#                                 come attached to BENEFITS far more often than to
#                                 arrangements — "WFH allowance", "WFH setup
#                                 provided", "WFH on Fridays". Two of the three
#                                 remote verdicts in the first live dry run came
#                                 from bare "WFH" and neither survived review.
#   remote role/position (bare)   fine when framed ("this is a remote role") and
#                                 not when merely mentioned ("we have remote roles
#                                 across the company"). Only the framed form is
#                                 here.
#   "you may work remotely"       `may` is permission, not arrangement.
#   occasional/some remote        a hybrid week described loosely at best.
#
# The cost of the strictness is on the record and was measured: it labels ZERO
# rows of the current live board remote, including a Databricks posting whose
# LOCATION field reads "Remote - India" but whose prose never says so. That job is
# a miss, and a miss is the error this file is willing to make.
_REMOTE_FRAMED_RE = re.compile(
    # An unambiguous intensifier, which no benefits list produces.
    r"\b(?:100\s*%|fully|entirely|completely|permanently)\s*[- ]?\s*remote\b"
    # The posting saying it about itself, in so many words.
    r"|\b(?:this|the)\s+(?:role|position|job|opportunity|opening)\s+(?:is|will\s+be)\s+"
    r"(?:an?\s+)?(?:fully\s+|100%\s+|entirely\s+)?remote\b"
    r"|\b(?:this\s+)?is\s+(?:an?\s+)?(?:fully\s+)?remote\s+(?:role|position|job|opening)\b"
    r"|\byou\s+will\s+(?:be\s+)?work(?:ing)?\s+(?:100%\s+|fully\s+)?remotely\b"
    # Working from anywhere is a location claim and cannot be a perk in disguise.
    r"|\bwork\s+from\s+anywhere\b|\bbased\s+anywhere\s+in\s+india\b"
    # A structured line inside the prose — the vendor's own field, spelled out.
    r"|\b(?:work\s+)?(?:location|workplace(?:\s+type)?)\s*[:\-]\s*remote\b"
    r"|\bremote\s*\(\s*(?:india|anywhere)\s*\)",
    re.IGNORECASE,
)

# In a TITLE the term needs no framing, exactly as _PM_TITLE_RE does not: a
# posting called "Software Engineer (Remote)" is not discussing remote servers.
# Anchored on the bracket or dash a title actually uses, so "Remote Sensing
# Engineer" is untouched.
_TITLE_REMOTE_RE = re.compile(r"[(\[|,-]\s*(?:100%\s*)?remote\b|\bremote\s*[)\]]", re.IGNORECASE)
_TITLE_HYBRID_RE = re.compile(r"[(\[|,-]\s*hybrid\b|\bhybrid\s*[)\]]", re.IGNORECASE)


def explain_work_mode(title: Any, text: Any) -> tuple[str | None, str | None, str | None]:
    """(mode, the phrase that decided it, a snippet of context around it).

    The verdict AND the evidence, for the same reason
    fetchers.normalize.explain_label returns its marker: a person reviewing a
    backfill has to be able to see WHY a row was called remote, and re-deriving
    that in the report would be a second implementation of the rule.

    marker None with a mode of "onsite" means "read it, and it stated no
    arrangement" — the specified default, which is a rule and not a shrug. Mode
    None means there was no text at all.
    """
    if isinstance(title, str) and title.strip():
        if (hit := _TITLE_HYBRID_RE.search(title)) is not None:
            return "hybrid", hit.group(0).strip(), title.strip()[:160]
        if (hit := _TITLE_REMOTE_RE.search(title)) is not None:
            return "remote", hit.group(0).strip(), title.strip()[:160]

    if not isinstance(text, str) or not text.strip():
        return None, None, None

    masked = _mask(_mask(text, _REMOTE_NOT_AN_ARRANGEMENT_RE), _HYBRID_NOT_AN_ARRANGEMENT_RE)

    for mode, pattern in (("hybrid", _HYBRID_FRAMED_RE), ("remote", _REMOTE_FRAMED_RE)):
        hit = pattern.search(masked)
        if hit is None:
            continue
        # The snippet comes from the ORIGINAL text, not the masked copy, because
        # it is for a person to read. Same offsets — that is what same-length
        # masking buys.
        start, end = max(0, hit.start() - 90), min(len(text), hit.end() + 90)
        snippet = " ".join(text[start:end].split())
        return mode, hit.group(0).strip(), f"…{snippet}…"

    return DEFAULT_WORK_MODE, None, None


def detect_work_mode(title: Any, text: Any) -> str | None:
    """onsite | hybrid | remote from the description, or None if there was none.

    HYBRID IS TESTED BEFORE REMOTE, and it is the precedence that matters. A
    hybrid posting nearly always contains remote language — "two days remote,
    three in the office" — and reading it as fully remote would relax a city gate
    that this posting does not relax. Same shape as navi-mumbai being scanned
    ahead of mumbai in fetchers/normalize.py.

    None means no description was readable, which is NOT "onsite". The column
    stays NULL, notify.match.work_mode_of reads that NULL as onsite for the
    purpose of filtering, and the difference between the two is preserved on the
    row — the jobs.notified lesson: one column, one fact. "We read it and it said
    nothing" and "we never managed to read it" are different facts, and the
    second is the one desc_fetch_failed already tracks.
    """
    return explain_work_mode(title, text)[0]


# --- salary: what the posting says it pays ---------------------------------
#
# THE NOISIEST EXTRACTION IN THIS FILE, AND THE ONE WITH THE LEAST TO GAIN.
#
# Measured before it was written, over the 102 rows the enrichment seam actually
# reaches: 36 of 36 descriptions were read cleanly and NOT ONE stated a number.
# 22 of the 36 contained the WORDS salary / compensation / CTC. So the realistic
# yield of this parser on today's board is zero, and its realistic failure mode is
# reading one of those 22 as a wage. It is built for the day an Indian board starts
# publishing pay, and until then the only thing it can do is be wrong.
#
# That asymmetry decides every rule below. A wrong number does not merely mislabel
# a row: preferences.salary_bands is a membership test, so a job priced at a figure
# nobody ticked is a job that DISAPPEARS. Silence, by contrast, shows the job —
# notify.match.salary_ok treats an unpriced posting as always visible. So the whole
# module is arranged to answer "no salary stated" whenever it is not certain, and
# every rule here is a reason to say nothing.
#
# THE ANCHOR IS A CURRENCY OR A UNIT, NEVER A BARE NUMBER, and that single
# structural decision removes most of the false-positive surface by construction
# rather than by blocklist. Every one of these is a real string from the live
# candidate set, and none of them can reach a rule below because none carries ₹,
# Rs, INR, lakh, lac or crore:
#
#     "30,000+ employees worldwide"                Accenture   — headcount
#     "200,000 people in 56 countries"             Continental — headcount
#     "3000+ strong team"                          Razorpay    — headcount
#     "1,700+ Zetanauts"                           Zeta        — headcount
#     "trusted by over 2,500 of the world's ..."   HackerRank  — customers
#     "30,000+ brands and leading publishers"      InMobi      — customers
#     "the 200,000th candidate"                    HackerRank  — an ordinal
#     "Founded in 1871"  /  "In 2023"  /  "2024"   Continental — dates
#     "call +1 408-536-3015"                       Adobe       — a phone number
#     "5 years of experience"                      everywhere  — a duration
#
# A parser that read bare numbers would find a salary in every one of them. This
# one cannot see them at all, which is a stronger guarantee than a list of things
# to avoid — the list can be incomplete, the anchor cannot.
#
# THE VETOES BELOW ARE THEREFORE ONLY FOR REAL MONEY THAT IS NOT THIS JOB'S PAY:
# an ESOP grant, a funding round, an insurance cover, a foreign figure, an
# application-form field. Those DO carry a currency, so the anchor lets them
# through and something else has to stop them.

# The unit everything normalises to: lakhs per annum. Decimal rather than float —
# the value lands in a NUMERIC column, and 18.5 LPA should round-trip as 18.5.
LAKH = Decimal(100_000)

# Nobody in this market is hired at under a lakh a year or over two crore, and a
# figure outside that is evidence the match was not a wage at all. This is what
# quietly kills funding announcements: "raised ₹500 crore" normalises to 50,000
# LPA and fails here even if every veto below missed it.
MIN_PLAUSIBLE_LPA = Decimal(1)
MAX_PLAUSIBLE_LPA = Decimal(200)

_INR = r"(?:₹|rs\.?|inr)"
_LAKH_UNIT = r"(?:lpa|lakhs?|lacs?)"
_CRORE_UNIT = r"(?:crores?|crs?\b)"

# "LAKH CRORE" IS ONE UNIT AND IT IS NOT LAKHS, and this lookahead is here
# because the first live dry run priced five PhonePe postings at 150 LPA from:
#
#     "…transactions daily with an Annualized Total Payment Value (TPV) of over
#      INR 150 lakh crore."
#
# The lakh pattern matched "INR 150 lakh", stopped, and never saw the word that
# changes the magnitude by seven orders. 150 lakh crore is ₹1.5 quadrillion — a
# payments company's annual volume, and the single largest number in the whole
# candidate set. It read as a plausible senior salary because the parser had
# truncated it into one.
#
# Every lakh form therefore refuses to match when `crore` follows. Note what that
# does rather than what it might have done: the compound is not re-read at crore
# scale, it is not read AT ALL, because _SALARY_SINGLE_CRORE_RE requires the
# digits to be adjacent to `crore` and "lakh" sits between them. Silence is the
# right answer for a company-metrics figure, and it is reached here by the
# number's own shape rather than by a veto having to recognise "TPV".
_NOT_LAKH_CRORE = r"(?!\s*crores?\b)"

# EQUITY IS NOT PAY, and it is the false positive named most often for a reason:
# Indian offer prose puts the two side by side, and the equity number is usually
# the bigger one. "₹50,00,000 ESOP" beside "₹18,00,000 fixed" would be read as a
# 50 LPA job by anything that took the largest figure, or a coin flip by anything
# that took the first.
#
# Both directions are matched because both are written: the grant can lead
# ("ESOPs worth ₹20 lakhs") or trail ("₹50,00,000 in stock options").
_SALARY_EQUITY_RE = re.compile(
    r"\b(?:esops?|equity|stock\s+options?|rsus?|restricted\s+stock|shares?|"
    r"share\s+options?)\b[^.\n]{0,40}?"
    + _INR
    + r"\s*[\d.,]+\s*(?:" + _LAKH_UNIT + r"|" + _CRORE_UNIT + r"|k|l)?"
    r"|" + _INR + r"?\s*[\d.,]+\s*(?:" + _LAKH_UNIT + r"|" + _CRORE_UNIT + r"|k|l)?"
    r"[^.\n]{0,25}?\b(?:esops?|equity|stock\s+options?|rsus?|restricted\s+stock)\b",
    re.IGNORECASE,
)

# A COMPANY'S MONEY IS NOT AN OFFER. Every Indian startup JD opens with the
# funding paragraph — "raised $50M", "valued at ₹8,000 crore", "₹1,200 crore in
# revenue" — and two of these were in the live sample ("sales of €41.4 billion",
# "a valuation of $2 billion"). The plausibility ceiling catches most of them by
# arithmetic; this catches the ones that happen to land in a believable range,
# like a ₹40 lakh seed round.
#
# BOTH DIRECTIONS, because both are written and the first draft only had one.
# "we raised ₹40 lakh" puts the word first; "₹1,200 crore in annual revenue" puts
# it last, and that second shape slipped straight through until a test caught it.
# The equity and benefit vetoes above are two-directional for the same reason —
# English puts the qualifier on whichever side reads better.
_SALARY_FUNDING_TERM = (
    r"(?:raised|raising|funding|funded|valuation|valued|revenue|revenues|sales|"
    r"turnover|arr|gmv|profit|invested|investment|investments|round|series\s+[a-f]|"
    r"market\s+cap|assets\s+under\s+management|aum|ipo|acquisition|acquired|"
    r"tpv|gtv|gmv|total\s+payment\s+value|payment\s+value|transaction\s+value|"
    r"volume|volumes|throughput|disbursed|disbursements|processed)"
)

_SALARY_FUNDING_RE = re.compile(
    r"\b" + _SALARY_FUNDING_TERM + r"\b[^.\n]{0,40}?"
    + r"(?:" + _INR + r"|\$|€|£)?\s*[\d.,]+\s*"
    + r"(?:" + _LAKH_UNIT + r"|" + _CRORE_UNIT + r"|k|m|bn|billion|million|trillion)\b"
    r"|" + r"(?:" + _INR + r"|\$|€|£)?\s*[\d.,]+\s*"
    + r"(?:" + _LAKH_UNIT + r"|" + _CRORE_UNIT + r"|k|m|bn|billion|million|trillion)\b"
    + r"[^.\n]{0,40}?\b" + _SALARY_FUNDING_TERM + r"\b",
    re.IGNORECASE,
)

# A BENEFIT IS NOT A SALARY, and this is the veto most likely to earn its keep the
# day an Indian board does start publishing pay — because benefits are published
# NOW. "₹5,00,000 medical insurance cover", "a joining bonus of ₹1 lakh",
# "₹50,000 relocation allowance", "₹25,000 annual learning budget" are ordinary
# lines in a perks section, and every one of them is a real rupee figure that is
# not the wage.
#
# The same shape as _LEADING_DURATION_RE's _NOT_A_CAREER lookahead, one column
# over: a perks list is formatted exactly like a compensation section, so position
# and currency together are not enough to tell them apart.
_SALARY_BENEFIT_RE = re.compile(
    r"\b(?:insurance|cover|coverage|bonus|allowance|reimbursement|reimburse|budget|"
    r"stipend|award|prize|gratuity|pf|provident|gift|voucher|discount|subsidy|"
    r"relocation|referral|wellness|medical|health|learning|education|tuition|"
    r"donation|contribution|fee|fees|penalty|fine|deposit|cost|price|premium)\b"
    r"[^.\n]{0,30}?" + _INR + r"\s*[\d.,]+\s*(?:" + _LAKH_UNIT + r"|" + _CRORE_UNIT + r"|k|l)?"
    r"|" + _INR + r"\s*[\d.,]+\s*(?:" + _LAKH_UNIT + r"|" + _CRORE_UNIT + r"|k|l)?"
    r"[^.\n]{0,30}?\b(?:insurance|cover|coverage|bonus|allowance|reimbursement|budget|"
    r"stipend|award|prize|gratuity|gift|voucher|discount|subsidy|relocation|referral|"
    r"donation|contribution|fee|penalty|fine|deposit|premium)\b",
    re.IGNORECASE,
)

# FOREIGN MONEY IS MASKED RATHER THAN CONVERTED, and refusing the conversion is
# the decision rather than an omission.
#
# An FX rate is a number that changes, and putting one in this file would freeze
# it: the LPA figure written today would be wrong for as long as the row lives,
# in a column nothing re-derives. Worse, the arithmetic works — a $180,000 US
# posting converts to roughly 150 LPA, lands squarely in the `50+` band, and reads
# as the best job on the board while being in Seattle. The bands are an INDIAN pay
# ladder and a converted figure is not a point on it.
#
# Masked rather than fatal, so "₹18 LPA (about $21k)" still parses as 18 and a
# posting quoted only in dollars simply states no pay we can use. Both are the
# safe direction: unpriced shows the job.
#
# Found live: the Anthropic Greenhouse posting embeds <div class="pay-range">
# €220.000</div>, which is a genuine advertised salary in a currency this tool has
# no opinion about.
_SALARY_FOREIGN_RE = re.compile(
    r"(?:us\s*\$|a\$|s\$|c\$|hk\$|\$|€|£|¥|₩|₽|"
    r"\b(?:usd|eur|gbp|sgd|aud|cad|aed|sar|jpy|chf|cny|myr|hkd|nzd|zar)\b)"
    r"\s*[\d.,]+\s*(?:k|m|mn|bn|million|billion|lpa|lakhs?)?"
    r"|[\d.,]+\s*(?:k|m|mn)?\s*"
    r"\b(?:usd|eur|gbp|sgd|aud|cad|aed|sar|jpy|chf|cny|myr|hkd|nzd|zar)\b",
    re.IGNORECASE,
)

# THE APPLICATION FORM IS NOT THE OFFER, and on Greenhouse this is not a hazard
# but a certainty. _greenhouse_description joins questions[].label into the text
# on purpose (a form question is often the role describing itself), and 46 of the
# 102 enrichable rows are Greenhouse. Both of these are verbatim from the live
# sample:
#
#     "What are your salary expectations?"                          HackerRank
#     "Current Compensation (Please mention breakup of Fixed +
#      Variable + ESOPs)  Expected Comp"                            Razorpay
#
# They ask the CANDIDATE for a number. A posting that also states its own pay
# would put it in the description, not in a form label — so masking the question
# and everything to the end of its line cannot cost a real figure, and it stops
# the one place a number is guaranteed to mean somebody else's salary.
#
# The candidate's-history discipline of _REMOTE_NOT_AN_ARRANGEMENT_RE, applied to
# money: same mistake, different column.
_SALARY_FORM_QUESTION_RE = re.compile(
    r"\b(?:current|expected|desired|preferred|last\s+drawn|present)\s+"
    r"(?:annual\s+|total\s+|fixed\s+|gross\s+|in[- ]hand\s+)?"
    r"(?:salary|compensation|comp|ctc|package|pay|remuneration)\b[^\n]*"
    r"|\b(?:salary|compensation|ctc|pay|package)\s+expectation[s]?\b[^\n]*"
    r"|\bwhat\s+(?:are|is)\s+your\b[^\n]*\b(?:salary|compensation|ctc|pay)\b[^\n]*",
    re.IGNORECASE,
)

# A RATE IS NOT AN ANNUAL WAGE. "₹500/hour", "₹8,000 per day" are contractor
# terms, and annualising them needs an assumption about how many hours a year the
# engagement runs — which is a guess, and this module does not guess. Monthly is
# the one period that IS annualised, below, because twelve months is a year by
# definition rather than by assumption.
_SALARY_RATE_RE = re.compile(
    _INR + r"?\s*[\d.,]+\s*(?:k)?\s*"
    r"(?:/|per\s+|a\s+)\s*(?:hour|hr|day|week|session|class|project|assignment)\b",
    re.IGNORECASE,
)

# Ordered, and each one names itself so scripts/backfill_salary.py can report
# WHICH veto fired rather than only that the row produced nothing. The reviewable
# unit is the veto, exactly as the work-mode backfill's reviewable unit is the
# marker.
SALARY_VETOES: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    ("application-form question", _SALARY_FORM_QUESTION_RE),
    ("equity/ESOP", _SALARY_EQUITY_RE),
    ("funding/revenue/valuation", _SALARY_FUNDING_RE),
    ("benefit/bonus/insurance", _SALARY_BENEFIT_RE),
    ("foreign currency", _SALARY_FOREIGN_RE),
    ("hourly/daily rate", _SALARY_RATE_RE),
)

# A monthly figure, annualised. The window is short and cannot cross a newline,
# for _YEARS_RE's reason: "₹80,000" on one bullet and "per month" on the next are
# two statements, and joining them invents a third.
_PER_MONTH = r"(?:/|per\s+|a\s+|p\.?\s?m\.?\b|every\s+)\s*month|monthly|per\s+month"
_PER_MONTH_RE = re.compile(r"^[ \t]*(?:" + _PER_MONTH + r")", re.IGNORECASE)

# RANGES ARE SCANNED FIRST AND THEN MASKED OUT, so the single-figure patterns
# below cannot re-read "18" and "24" out of "18-24 LPA" as two separate salaries
# and then refuse the row for stating two. Same ordering discipline
# detect_min_experience uses between its range and single forms.
_SALARY_RANGE_LAKH_RE = re.compile(
    _INR + r"?\s*(\d{1,3}(?:\.\d{1,2})?)\s*(?:" + _LAKH_UNIT + r"|l\b)?"
    r"\s*(?:-|–|—|to|and)\s*"
    + _INR + r"?\s*(\d{1,3}(?:\.\d{1,2})?)\s*(" + _LAKH_UNIT + r"|l\b)" + _NOT_LAKH_CRORE,
    re.IGNORECASE,
)

_SALARY_RANGE_CRORE_RE = re.compile(
    _INR + r"?\s*(\d{1,2}(?:\.\d{1,2})?)\s*(?:" + _CRORE_UNIT + r")?"
    r"\s*(?:-|–|—|to|and)\s*"
    + _INR + r"?\s*(\d{1,2}(?:\.\d{1,2})?)\s*(" + _CRORE_UNIT + r")",
    re.IGNORECASE,
)

# The raw-rupee range needs the currency marker on at least the FIRST bound —
# without it "1,800,000 - 2,400,000" is just two numbers, and this module does not
# read bare numbers.
_SALARY_RANGE_RAW_RE = re.compile(
    _INR + r"\s*([\d,]{5,})\s*(?:-|–|—|to)\s*" + _INR + r"?\s*([\d,]{5,})",
    re.IGNORECASE,
)

_SALARY_SINGLE_LAKH_RE = re.compile(
    _INR + r"?\s*(\d{1,3}(?:\.\d{1,2})?)\s*\+?\s*(" + _LAKH_UNIT + r")" + _NOT_LAKH_CRORE,
    re.IGNORECASE,
)

# The bare `L` suffix ("18L") is accepted ONLY with a currency marker in front of
# it. Without one it is not distinguishable from a level notation — "L4", "L5" and
# "18L" live in the same documents — and detect_level exists one module over
# precisely because titles are full of numbers attached to letters.
_SALARY_SINGLE_L_RE = re.compile(
    _INR + r"\s*(\d{1,3}(?:\.\d{1,2})?)\s*\+?\s*l\b" + _NOT_LAKH_CRORE,
    re.IGNORECASE,
)

_SALARY_SINGLE_CRORE_RE = re.compile(
    _INR + r"?\s*(\d{1,2}(?:\.\d{1,2})?)\s*\+?\s*(" + _CRORE_UNIT + r")",
    re.IGNORECASE,
)

_SALARY_SINGLE_RAW_RE = re.compile(
    _INR + r"\s*([\d,]{5,})(?!\s*(?:-|–|—|to))",
    re.IGNORECASE,
)

# `₹80k`, `₹80 k`. Thousands, so ×1000 before the lakh division.
_SALARY_SINGLE_K_RE = re.compile(
    _INR + r"\s*(\d{1,4}(?:\.\d{1,2})?)\s*k\b",
    re.IGNORECASE,
)


class SalaryRead(NamedTuple):
    """A salary verdict with its evidence, for a person reviewing a backfill.

    `low`/`high` in LPA, both None when nothing was established. `marker` is the
    literal text that produced the figure and `snippet` its surroundings, for the
    same reason fetchers.normalize.explain_label returns its marker: a reviewer has
    to be able to see WHY a row was priced, and re-deriving it in the report would
    be a second implementation of the rule.

    `vetoes` names every guard that fired on this description whether or not a
    salary survived — that is what makes the guards auditable instead of merely
    present.
    """

    low: "Decimal | None" = None
    high: "Decimal | None" = None
    marker: str | None = None
    snippet: str | None = None
    vetoes: tuple[str, ...] = ()


def _lpa(value: str, unit: str) -> "Decimal | None":
    """One captured number plus its unit -> LPA, or None if implausible.

    The plausibility test lives here rather than at the call sites so that every
    form — lakh, crore, raw rupees, thousands, monthly — is bounded by the same
    two numbers. A figure that fails it is not a wage, whatever shape it arrived
    in.
    """
    try:
        amount = Decimal(value.replace(",", "").strip())
    except (InvalidOperation, AttributeError):
        return None

    unit = unit.lower()
    if unit == "lakh":
        lpa = amount
    elif unit == "crore":
        lpa = amount * 100
    elif unit == "rupees":
        lpa = amount / LAKH
    elif unit == "thousand":
        lpa = amount * 1000 / LAKH
    else:  # pragma: no cover — the caller passes one of the four
        return None

    if not (MIN_PLAUSIBLE_LPA <= lpa <= MAX_PLAUSIBLE_LPA):
        return None

    # TWO DECIMAL PLACES, AND NOT .normalize() ON ITS OWN. normalize() turns 150
    # into Decimal('1.5E+2') — numerically identical and unreadable, which matters
    # because this value is printed in the backfill sample a person reviews and
    # interpolated into notify.match.explain's reason string, where the TypeScript
    # port has to produce the same characters. Integral figures come back
    # integral, fractional ones keep at most two places.
    lpa = lpa.quantize(Decimal("0.01"))
    integral = lpa.to_integral_value()
    return integral if lpa == integral else lpa.normalize()


def _figure_to_lpa(raw: str, unit: str, monthly: bool) -> "Decimal | None":
    """One captured figure, its unit, and whether a period marker followed it.

    A MONTHLY LAKH FIGURE IS REFUSED RATHER THAN ANNUALISED. "18 LPA per month"
    is not a sentence anybody writes about a wage — LPA already says per annum —
    so a match of that shape is evidence the regex found something other than a
    salary, and the answer to that is silence. Only the two unit-less forms
    (raw rupees, thousands) are annualised, and twelve months is a year by
    definition rather than by assumption, which is why the hourly and daily forms
    are vetoed instead of multiplied.
    """
    if not monthly:
        return _lpa(raw, unit)

    if unit == "rupees":
        multiplier = Decimal(12)
    elif unit == "thousand":
        multiplier = Decimal(12_000)
    else:
        return None

    try:
        annual = Decimal(raw.replace(",", "").strip()) * multiplier
    except (InvalidOperation, AttributeError):
        return None
    return _lpa(str(annual), "rupees")


def _is_monthly(text: str, end: int) -> bool:
    """Does a period marker follow this figure, on the same line?

    Bounded to the same line for _YEARS_RE's reason: "₹80,000" ending a bullet and
    "per month" opening the next are two statements, and reading across the break
    invents a third.
    """
    line_end = text.find("\n", end)
    tail = text[end:] if line_end == -1 else text[end:line_end]
    return bool(_PER_MONTH_RE.search(tail[:20]))


def explain_salary(text: Any) -> SalaryRead:
    """The pay this description states, in LPA, with the evidence and the vetoes.

    THE ORDER IS VETO, THEN RANGE, THEN SINGLE, and each stage masks what it
    consumed so the next cannot re-read it. _mask is the same same-length blanking
    idiom the work-mode tier uses, for the same reason: a description can state an
    ESOP grant in one sentence and a wage in the next, and both a whole-text veto
    and a first-hit-wins scan would get one of those two wrong.

    SEVERAL DISAGREEING FIGURES MEAN NOTHING WAS ESTABLISHED, which is the one
    place this function deliberately does the opposite of detect_min_experience.
    That one takes min(found), because the smallest minimum is the answer that
    KEEPS the job and a keep is the safe error. There is no safe number here: any
    figure can put the job in a band nobody ticked and delete it, while silence
    shows it. So ambiguity resolves to silence.

    Identical restatements are not disagreement. "₹18,00,000 – ₹24,00,000 (18-24
    LPA)" is one fact written twice and normalises to one (18, 24).
    """
    if not isinstance(text, str) or not text.strip():
        return SalaryRead()

    fired: list[str] = []
    masked = text
    for name, pattern in SALARY_VETOES:
        if pattern.search(masked):
            fired.append(name)
            masked = _mask(masked, pattern)

    found: dict[tuple[str, str], tuple[int, int]] = {}

    def record(low: "Decimal | None", high: "Decimal | None", span: tuple[int, int]) -> None:
        if low is None and high is None:
            return
        found.setdefault((str(low), str(high)), span)

    for pattern, unit in (
        (_SALARY_RANGE_CRORE_RE, "crore"),
        (_SALARY_RANGE_LAKH_RE, "lakh"),
        (_SALARY_RANGE_RAW_RE, "rupees"),
    ):
        for hit in pattern.finditer(masked):
            monthly = _is_monthly(masked, hit.end())
            low = _figure_to_lpa(hit.group(1), unit, monthly)
            high = _figure_to_lpa(hit.group(2), unit, monthly)
            if low is None or high is None or low > high:
                continue
            record(low, high, hit.span())
        masked = _mask(masked, pattern)

    for pattern, unit in (
        (_SALARY_SINGLE_CRORE_RE, "crore"),
        (_SALARY_SINGLE_LAKH_RE, "lakh"),
        (_SALARY_SINGLE_L_RE, "lakh"),
        (_SALARY_SINGLE_K_RE, "thousand"),
        (_SALARY_SINGLE_RAW_RE, "rupees"),
    ):
        for hit in pattern.finditer(masked):
            value = _figure_to_lpa(hit.group(1), unit, _is_monthly(masked, hit.end()))
            if value is None:
                continue
            record(value, value, hit.span())
        masked = _mask(masked, pattern)

    if len(found) != 1:
        # Nothing found, or several statements that do not agree. Both mean the
        # same thing to every caller: this posting states no pay we are sure of.
        return SalaryRead(vetoes=tuple(fired))

    (low_text, high_text), (start, end) = next(iter(found.items()))
    marker = " ".join(text[start:end].split())
    window_start, window_end = max(0, start - 80), min(len(text), end + 80)
    snippet = " ".join(text[window_start:window_end].split())
    return SalaryRead(
        low=Decimal(low_text),
        high=Decimal(high_text),
        marker=marker,
        snippet=f"…{snippet}…",
        vetoes=tuple(fired),
    )


def detect_salary(text: Any) -> tuple["Decimal | None", "Decimal | None"]:
    """(low, high) in LPA, or (None, None) for "this posting states no pay".

    The narrow question asked of explain_salary's machinery, kept as its own name
    because it is the whole of what the enrichment pass wants — the same
    relationship fetch_description has with read_description.
    """
    read = explain_salary(text)
    return read.low, read.high


# THE STRUCTURAL TIER, exactly as work_mode_from_row is and for the same reason: a
# vendor that STATES the pay is a better authority on it than any regex over the
# prose underneath, and reading it costs nothing because the row is already here.
#
# VERIFIED LIVE, NOT READ FROM DOCUMENTATION — the discipline the Workday country
# facet earned. Of the nine supported ATSes, exactly two publish a comp field:
#
#   ashby      compensation.summaryComponents[] — but ONLY when the board URL
#              carries ?includeCompensation=true, which is why _BOARD_URLS spells
#              it that way. Confirmed against the live `navi` and `atlan` boards:
#              the key appears on every job, alongside
#              shouldDisplayCompensationOnJobPostings, and both tenants publish
#              empty tiers because they have the display switched off.
#   recruitee  salary {min, max, period, currency}
#
# AND THE OTHER SEVEN PUBLISH NOTHING, which is the fact that matters most here:
# greenhouse (checked live against a single posting with ?content=true — the keys
# are content/metadata/offices/questions and no pay range), workday, smartrecruiters
# and lever all carry no comp field at all, and those four are 100% of the rows the
# enrichment seam currently reaches. So this tier is CORRECT AND INERT on today's
# board, and the prose tier above is doing all of the work.
def salary_from_row(
    ats: Any, row: Mapping[str, Any] | None
) -> tuple["Decimal | None", "Decimal | None"]:
    """The ATS's own salary field in LPA, or (None, None) if it publishes none.

    NON-INR IS REFUSED HERE TOO, and by the same argument the prose tier makes: a
    vendor stating $180,000 is stating a fact this tool cannot convert into a
    point on an Indian band ladder without freezing an exchange rate into a column
    nothing re-derives.
    """
    ats = str(ats or "").strip().lower()
    if not isinstance(row, Mapping):
        return None, None

    if ats == "ashby":
        compensation = row.get("compensation")
        if not isinstance(compensation, Mapping):
            return None, None
        for component in compensation.get("summaryComponents") or []:
            if not isinstance(component, Mapping):
                continue
            if str(component.get("compensationType") or "").strip().lower() != "salary":
                continue
            if str(component.get("currencyCode") or "").strip().upper() != "INR":
                continue
            interval = str(component.get("interval") or "").strip().upper()
            per_year = {"1 YEAR": 1, "1 MONTH": 12}.get(interval)
            if per_year is None:
                continue
            low = _amount_to_lpa(component.get("minValue"), per_year)
            high = _amount_to_lpa(component.get("maxValue"), per_year)
            if low is None and high is None:
                continue
            return (low or high), (high or low)
        return None, None

    if ats == "recruitee":
        salary = row.get("salary")
        if not isinstance(salary, Mapping):
            return None, None
        if str(salary.get("currency") or "").strip().upper() != "INR":
            return None, None
        per_year = {"year": 1, "yearly": 1, "annual": 1, "month": 12, "monthly": 12}.get(
            str(salary.get("period") or "").strip().lower()
        )
        if per_year is None:
            return None, None
        low = _amount_to_lpa(salary.get("min"), per_year)
        high = _amount_to_lpa(salary.get("max"), per_year)
        if low is None and high is None:
            return None, None
        return (low or high), (high or low)

    return None, None


def _amount_to_lpa(value: Any, per_year: int) -> "Decimal | None":
    """A vendor's raw rupee amount at some period -> LPA, bounded like the rest."""
    if value is None:
        return None
    try:
        amount = Decimal(str(value)) * per_year
    except (InvalidOperation, ValueError):
        return None
    return _lpa(str(amount), "rupees")


# --- fetching the description ---------------------------------------------
#
# Two shapes of source, and the difference is one request versus zero.
#
# Lever, Ashby, Workable (details=true) and Recruitee all ship the description
# inside the same list payload the crawler already knows how to ask for, so the
# whole board is fetched once and every candidate on it is answered from memory.
#
# Greenhouse and SmartRecruiters are the other shape: their list endpoints carry
# no description at all, so each candidate costs one request. Greenhouse's
# fetchers/tier1.py leaves ?content=true off precisely because it inflates every
# row with megabytes we never store. Verified live against
# boards-api.greenhouse.io/v1/boards/{token}/jobs/{id}?content=true, which
# answers 200 with an HTML-escaped `content` field.

GREENHOUSE_JOB_URL = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{job_id}"

# SmartRecruiters' single-posting route. The board endpoint tier1.py pages
# through returns summary rows only — `name`, `location`, `department`,
# `experienceLevel` and no prose — so the description has to be asked for per
# job, exactly as with Greenhouse. Verified live, unauthenticated, 200:
#
#   https://api.smartrecruiters.com/v1/companies/{token}/postings/{id}
#     -> {"jobAd": {"sections": {"companyDescription": {"title", "text"},
#                               "jobDescription":     {...},
#                               "qualifications":     {...},
#                               "additionalInformation": {...}}}, …}
#
# This ATS had NO route here until now, and the cost of that was not theoretical:
# 12 of the 46 live matches — 10 Continental, 1 Lendingkart, 1 Unacademy — came
# back with nothing, and `unknown` means KEEP, so every one of them was in the
# digest on no evidence about its experience requirement. That is precisely the
# state the InMobi row was in.
SMARTRECRUITERS_JOB_URL = "https://api.smartrecruiters.com/v1/companies/{token}/postings/{job_id}"

# All four sections, in the order the ad reads them.
#
# `qualifications` is the load-bearing one and the reason a partial read would be
# worse than none: it is where every years-of-experience line lives, the same way
# Lever keeps requirements in `lists` rather than in `description`. Reading only
# `jobDescription` would make every SmartRecruiters posting look like it stated
# no experience at all — which is the bug this route exists to fix, reintroduced
# one field lower down.
#
# `additionalInformation` is the perks bucket ("Attraktive Vergütung: …", a
# benefits list) and is included anyway, for the same reason fetch of a Workable
# board reads `benefits`: a real posting does sometimes state its requirement
# there, and the detectors are built to survive a perks list rather than to be
# kept away from one. _LEADING_DURATION_RE's _NOT_A_CAREER lookahead is exactly
# that defence — "5 years of service award" leads a bullet in this section and
# must not read as a five-year minimum.
_SR_SECTIONS = ("companyDescription", "jobDescription", "qualifications", "additionalInformation")

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
#
# ASHBY'S URL DIFFERS FROM fetchers/tier1.py's BY ONE QUERY PARAMETER, and the
# divergence is deliberate rather than drift. `?includeCompensation=true` is what
# makes compensation.summaryComponents appear at all (verified live: without it
# the key is absent entirely, with it every job carries it). It is added HERE and
# not in tier1 because tier1's fetch runs hourly for every tracked board and its
# job is the crawl's row shape, which has no salary in it — paying for a wider
# payload on the path that throws it away would be the ?content=true mistake the
# Greenhouse comment above describes, one vendor over.
_BOARD_URLS = {
    "lever": "https://api.lever.co/v0/postings/{token}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true",
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


async def _smartrecruiters_description(token: str, job_id: str) -> str | None:
    """One SmartRecruiters posting's description, from the public posting API.

    The section titles are joined in along with their bodies. That is not
    decoration: the heading "Qualifications" (or its localised form) is often the
    only thing marking where boilerplate ends and requirements begin, and it
    costs a line of text that is thrown away with the rest of the document a
    moment later.

    Each section's `text` is HTML, so it goes through html_to_text like every
    other vendor's markup — which is what turns `</li>` into the newline that
    _LEADING_DURATION_RE anchors on. Politeness is the shared default: the same
    client, semaphore, per-host pace and backoff as every other fetch here.
    """
    return (await _smartrecruiters_read(token, job_id)).text


async def _smartrecruiters_read(token: str, job_id: str) -> "DescriptionRead":
    """The posting's prose AND its `location.remote` / `location.hybrid` booleans.

    The two booleans sit on the same response the sections come from, so reading
    them is free. Verified live: a Continental posting answers
    `location: {remote: false, hybrid: false, ...}`, and both being false is a
    real "onsite" rather than a silence — which is why work_mode_from_row treats
    it as one.
    """
    res = await polite_get(SMARTRECRUITERS_JOB_URL.format(token=token, job_id=job_id))
    if not res.ok or not isinstance(res.json, dict):
        return DescriptionRead(None)

    mode = work_mode_from_row("smartrecruiters", res.json)

    ad = res.json.get("jobAd")
    sections = ad.get("sections") if isinstance(ad, dict) else None
    if not isinstance(sections, dict):
        return DescriptionRead(None, mode)

    parts: list[Any] = []
    for key in _SR_SECTIONS:
        section = sections.get(key)
        if not isinstance(section, dict):
            continue
        parts.append(section.get("title"))
        parts.append(html_to_text(section.get("text")))
    return DescriptionRead(_joined(parts), mode)


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


class DescriptionRead(NamedTuple):
    """What one read of a posting yielded: the prose, and the vendor's own fields.

    Several values because they come from two places and only one of them is a
    guess. `work_mode` is set when the ATS PUBLISHES the arrangement (ashby,
    lever, smartrecruiters — see work_mode_from_row) and is None otherwise, which
    is what makes "the structural answer wins over the prose one" expressible
    without either tier knowing about the other. The salary pair says the same
    thing about pay, for the two vendors that publish it (see salary_from_row).

    A NamedTuple rather than a second return value so that
    `enrich_candidates` can accept both this and a bare string: every test in
    this repo injects a fetcher returning `str | None`, and those injections are
    the point of the seam rather than an implementation detail to churn. Same
    accommodation notify.match.Preferences._as_tuple makes for asyncpg lists
    versus the SQLite shim's JSON text. Every field after `text` therefore has a
    default, so a two-field construction in a test keeps meaning what it meant.
    """

    text: str | None
    work_mode: str | None = None
    salary_min: "Decimal | None" = None
    salary_max: "Decimal | None" = None


async def read_description(
    company: Mapping[str, Any], job: Mapping[str, Any]
) -> DescriptionRead:
    """fetch_description, plus whatever the vendor stated about the arrangement.

    THE STRUCTURAL FIELDS COST NOTHING EXTRA, and that is why they are read here
    rather than in a pass of their own. All three ATSes that publish the
    arrangement publish it on the same row this function was already going to
    parse the description out of — lever and ashby from the cached board index,
    smartrecruiters from the single posting it fetches anyway. Greenhouse and
    Workday publish no such field, so they come back with work_mode None and the
    prose tier answers.

    THE SALARY PAIR IS NARROWER STILL: only ashby and recruitee publish one, both
    of them board-index vendors, so the whole structural tier for pay lives in the
    one branch below and every other ATS answers (None, None) and leaves the prose
    parser to it. On today's board that is every row the seam reaches.
    """
    ats = str(company.get("ats") or "").strip().lower()
    token = str(company.get("token") or "").strip()
    if not ats or not token:
        return DescriptionRead(None)

    native_id = native_id_from(job.get("canonical_id")) or job.get("native_id")

    if ats == "greenhouse":
        text = await _greenhouse_description(token, str(native_id)) if native_id else None
        return DescriptionRead(text)

    if ats == "smartrecruiters":
        if not native_id:
            return DescriptionRead(None)
        return await _smartrecruiters_read(token, str(native_id))

    if ats == "workday":
        return DescriptionRead(await _workday_description(company, job))

    if ats in _BOARD_URLS:
        if not native_id:
            return DescriptionRead(None)
        row = (await _board_index(ats, token)).get(str(native_id))
        if not row:
            return DescriptionRead(None)
        stated_low, stated_high = salary_from_row(ats, row)
        return DescriptionRead(
            description_from_row(ats, row),
            work_mode_from_row(ats, row),
            stated_low,
            stated_high,
        )

    # The custom scrapers (custom_jsonld, custom_sitemap) publish no description
    # route this module has verified. Unknown, therefore kept — and recorded as a
    # FAILED read rather than a clean one, so it is asked about again tomorrow
    # instead of being believed forever.
    return DescriptionRead(None)


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

    THE TEXT HALF OF read_description, kept as its own name because it is the
    whole of what most callers and every test injection want. Production goes
    through read_description; this is the narrower question asked of the same
    machinery, not a second route to it.
    """
    return (await read_description(company, job)).text


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

    What this writes is deliberately eight columns and no more — the five
    description facts, the salary pair and salary_checked: it must not be able to
    disturb `seeded`, `status`, `miss_count` or `first_seen_at`. See
    SAVE_DESCRIPTION_FACTS_SQL, which has no clause that could.

    Sequential rather than gathered, on purpose. The set is a handful by the
    time it gets here, the shared client already paces itself per host, and one
    request at a time is the version whose behaviour against someone else's
    board is obvious from reading it.
    """
    fetch = fetcher or read_description
    report: list[dict[str, Any]] = []
    facts: list[
        tuple[str, int | None, bool, bool, str | None, "Decimal | None", "Decimal | None", bool]
    ] = []

    for job in candidates:
        if not needs_description_check(job, retry_before=retry_before):
            continue

        # BOTH FETCHER SHAPES ACCEPTED. Production hands back a DescriptionRead;
        # every test injection in this repo hands back `str | None`, and those
        # injections are what the seam is FOR. A bare string means "no structural
        # field", which is exactly the state greenhouse and workday are in.
        read = await fetch(company_of(job), job)
        if not isinstance(read, DescriptionRead):
            read = DescriptionRead(read)
        text, stated_mode = read.text, read.work_mode

        failed = text is None
        min_experience = detect_min_experience(text)
        is_pm_role = detect_pm_role(job.get("title"), text)

        # THE VENDOR'S FIGURE WINS OVER THE PROSE, the same precedence work_mode
        # takes on the line below and for the same reason — a stated answer beats
        # an inferred one. `is not None` rather than truthiness: a vendor could in
        # principle state a zero, and `or` would silently discard it.
        parsed_low, parsed_high = detect_salary(text)
        salary_min = read.salary_min if read.salary_min is not None else parsed_low
        salary_max = read.salary_max if read.salary_max is not None else parsed_high

        # SET WHENEVER A DESCRIPTION WAS ACTUALLY READ, and never when the fetch
        # failed. That is the whole point of the column: "we looked and this
        # posting states no pay" is a fact, and it is a DIFFERENT fact from "no
        # parser has ever been near this row", which is what every row on this
        # board says today. See db/schema.sql — the two would otherwise share one
        # NULL, which is jobs.notified with a decimal point.
        salary_checked = not failed

        # THE VENDOR'S FIELD WINS OVER THE PROSE, and it wins even when the prose
        # could not be read at all: an ashby row whose description came back
        # empty still stated `workplaceType`, and a stated answer beats no answer.
        # detect_work_mode returns None for an unreadable description, which
        # leaves the column NULL — read as onsite at match time, never written.
        work_mode = stated_mode or detect_work_mode(job.get("title"), text)

        # The row dict is updated in place so the caller's very next is_match()
        # sees these facts without a re-read of the table.
        job["min_experience"] = min_experience  # type: ignore[index]
        job["is_pm_role"] = is_pm_role  # type: ignore[index]
        job["work_mode"] = work_mode  # type: ignore[index]
        job["salary_min"] = salary_min  # type: ignore[index]
        job["salary_max"] = salary_max  # type: ignore[index]
        job["salary_checked"] = salary_checked  # type: ignore[index]
        job["desc_checked"] = True  # type: ignore[index]
        job["desc_fetch_failed"] = failed  # type: ignore[index]

        facts.append(
            (
                job["canonical_id"],
                min_experience,
                is_pm_role,
                failed,
                work_mode,
                salary_min,
                salary_max,
                salary_checked,
            )
        )
        report.append(
            {
                "canonical_id": job["canonical_id"],
                "title": job.get("title"),
                "company": job.get("company"),
                "min_experience": min_experience,
                "is_pm_role": is_pm_role,
                "work_mode": work_mode,
                "work_mode_stated_by_ats": stated_mode is not None,
                "salary_min": salary_min,
                "salary_max": salary_max,
                "salary_stated_by_ats": read.salary_min is not None,
                "fetched": not failed,
            }
        )
        # `text` is rebound on the next iteration and never leaves this scope.

    await save_description_facts(conn, facts)
    return report
