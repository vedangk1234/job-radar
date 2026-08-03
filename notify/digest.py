"""Render the matched jobs into one email per person, and send through Resend.

One email per user, never several. A digest that arrives as eleven separate
messages is the same failure as an inbox full of noise — you stop opening it — so
each user's run either produces a single grouped message or produces nothing at
all.

ONE EMAIL PER USER IS PHASE 1e — the fan-out. `run_digest` is one person's
digest and `run_fan_out` is the loop over everybody, and the split is the whole
design: every statement underneath is bound to a single user_id, so isolation
between two users' digests is a property of the SQL rather than of this loop
being careful. The crawl above it is untouched and stays user-blind; only the
notification narrows. See run_fan_out.

And *nothing at all* is a real outcome here, not an error. Zero matches sends
zero emails. An empty digest that shows up every morning saying "no new jobs"
trains you to ignore the sender, which means the one morning it is not empty you
ignore that too.

Ordering, which is the safety property
--------------------------------------
    title filter -> read descriptions -> match -> render -> send
                 -> (Resend returns an id) -> record the notification, write the
                                              receipt

The LIFECYCLE write happens last and only on success. Since Phase 1d "record the
notification" means a row in user_job_notifications for THIS user — the boolean
is written alongside it in the same transaction as a rollback net, but the
per-user row is the thing that decides whether a job is ever considered again.
See notify/store.record_sent.

One write does happen before the send: notify/description.py stores the three
description-derived columns as soon as it computes them. That is not a hole in
the rule, because those columns say nothing about whether a job was sent. They
are facts about the posting, true whether or not an email goes out, and the only
cost of a run that dies after writing them is that the next run does not have to
re-read the same descriptions. No flag, no receipt — nothing that could make a
job look delivered when it was not. If Resend errors, times out, or
rejects the recipient, this module raises and touches no rows: no notification is
recorded and the next run picks up exactly the same set. That is the crawler's
Guard 1 philosophy applied to email — when the outcome is in doubt, change
nothing.

The accepted cost of that choice: if a send actually succeeded but the response
never reached us (a timeout after delivery), the next run re-sends those jobs.
A rare duplicate is the cheap failure; a job silently marked sent and never
delivered is the expensive one.

Resend is called over plain httpx rather than through the `resend` SDK — the
project already depends on httpx, the API is one POST, and going direct means
the response body is available verbatim when something is rejected, which is
what makes the onboarding@resend.dev recipient restriction diagnosable instead
of mysterious.
"""

from __future__ import annotations

import html
import os
from collections import OrderedDict
from datetime import date
from typing import Any, Iterable, Mapping

import httpx

from db.connection import load_env
from db.users import digest_users, email_for_user, resolve_current_user_id

from .description import enrich_candidates
from .match import Preferences, select_matches, title_stage_ok
from .store import fetch_candidates, load_preferences, record_sent

RESEND_ENDPOINT = "https://api.resend.com/emails"
SEND_TIMEOUT = httpx.Timeout(30.0, connect=15.0)

# Resend's sandbox sender. It is not a verified domain, so Resend will only
# deliver from it to the address that owns the API key — see assert_self_send.
SANDBOX_FROM = "onboarding@resend.dev"

CITY_NAMES = {
    "blr": "Bengaluru",
    "pune": "Pune",
    "hyd": "Hyderabad",
    "india-unclear": "India (unclear)",
    "other": "Other",
}


class DigestError(RuntimeError):
    """A send could not be attempted, or Resend refused it. Nothing was written."""


# --- grouping -------------------------------------------------------------


def group_by_company(jobs: Iterable[Mapping[str, Any]]) -> "OrderedDict[str, list[dict]]":
    """{company: [job, ...]} preserving the query's company-then-title order.

    Grouping is what makes the email skimmable: four headings beat nineteen
    undifferentiated rows, and seeing three roles from one employer together is
    itself information.
    """
    groups: OrderedDict[str, list[dict]] = OrderedDict()
    for job in jobs:
        groups.setdefault(job.get("company") or "(unknown company)", []).append(dict(job))
    return groups


def subject_line(jobs: list[Mapping[str, Any]], today: date | None = None) -> str:
    """The one line that decides whether the email gets opened.

    Leads with the count and names the employers, because "3 new matches" tells
    you whether to look now and "Groww, Postman" tells you whether to look at all.
    """
    groups = group_by_company(jobs)
    stamp = (today or date.today()).isoformat()
    count = len(jobs)
    noun = "match" if count == 1 else "matches"

    names = list(groups)
    if len(names) <= 3:
        who = ", ".join(names)
    else:
        who = ", ".join(names[:3]) + f" +{len(names) - 3} more"

    return f"job-radar: {count} new {noun} — {who} ({stamp})"


def _city_name(job: Mapping[str, Any]) -> str:
    city = job.get("city") or ""
    return CITY_NAMES.get(city, city or "unknown")


# --- rendering ------------------------------------------------------------
#
# Inline styles only, one 640px column, no external CSS and no images. Every
# mail client strips <style> blocks differently and half of them block remote
# content by default; inline attributes on simple block elements are the only
# thing that renders the same in Gmail's web view and on a phone.


def render_text(jobs: list[Mapping[str, Any]], prefs: Preferences, today: date | None = None) -> str:
    """Plain-text fallback. Not a courtesy — some clients show only this part."""
    groups = group_by_company(jobs)
    count = len(jobs)
    lines = [
        f"job-radar — {count} new {'match' if count == 1 else 'matches'}"
        f" ({(today or date.today()).isoformat()})",
        "",
    ]
    for company, company_jobs in groups.items():
        lines.append(f"{company} ({len(company_jobs)})")
        for job in company_jobs:
            lines.append(f"  - {job.get('title') or '(untitled)'}")
            lines.append(f"    {_city_name(job)} · {job.get('location') or 'location not stated'}")
            lines.append(f"    {job.get('url') or ''}")
        lines.append("")

    lines.append("-" * 48)
    lines.append("Filtered on cities: " + ", ".join(prefs.cities))
    lines.append("Edit these filters in the preferences table.")
    return "\n".join(lines)


def render_html(jobs: list[Mapping[str, Any]], prefs: Preferences, today: date | None = None) -> str:
    """One grouped, inline-styled, mobile-readable HTML digest."""
    groups = group_by_company(jobs)
    count = len(jobs)
    stamp = (today or date.today()).isoformat()
    esc = html.escape

    parts: list[str] = [
        '<div style="margin:0;padding:24px 12px;background:#f5f6f8;'
        'font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,Helvetica,Arial,sans-serif;'
        '-webkit-text-size-adjust:100%;">',
        '<div style="max-width:640px;margin:0 auto;background:#ffffff;border-radius:10px;'
        'border:1px solid #e3e5e9;overflow:hidden;">',
        # header
        '<div style="padding:20px 24px;border-bottom:1px solid #eceef1;">',
        '<div style="font-size:12px;letter-spacing:.09em;text-transform:uppercase;'
        'color:#8a8f98;font-weight:600;">job-radar</div>',
        f'<div style="font-size:20px;line-height:1.3;font-weight:700;color:#15171a;padding-top:4px;">'
        f'{count} new {"match" if count == 1 else "matches"}</div>',
        f'<div style="font-size:13px;color:#8a8f98;padding-top:2px;">'
        f'{esc(stamp)} · {len(groups)} {"company" if len(groups) == 1 else "companies"}</div>',
        "</div>",
    ]

    for company, company_jobs in groups.items():
        parts.append('<div style="padding:18px 24px 6px 24px;">')
        parts.append(
            '<div style="font-size:15px;font-weight:700;color:#15171a;'
            'padding-bottom:10px;border-bottom:1px solid #f0f1f4;">'
            f'{esc(company)}'
            f'<span style="font-weight:400;color:#a0a4ac;"> · {len(company_jobs)}</span></div>'
        )
        for job in company_jobs:
            title = esc(job.get("title") or "(untitled)")
            url = esc(job.get("url") or "#", quote=True)
            location = esc(job.get("location") or "location not stated")
            parts.append(
                '<div style="padding:12px 0;border-bottom:1px solid #f6f7f8;">'
                f'<a href="{url}" style="font-size:16px;line-height:1.35;font-weight:600;'
                'color:#1a56db;text-decoration:none;display:block;">'
                f'{title}</a>'
                '<div style="font-size:13px;line-height:1.5;color:#6b7079;padding-top:4px;">'
                f'<span style="display:inline-block;background:#eef2ff;color:#3d4fa3;'
                'border-radius:4px;padding:1px 7px;font-size:12px;font-weight:600;">'
                f'{esc(_city_name(job))}</span>'
                f'<span style="padding-left:8px;">{location}</span>'
                "</div></div>"
            )
        parts.append("</div>")

    parts.extend([
        '<div style="padding:16px 24px 22px 24px;font-size:12px;line-height:1.6;color:#9aa0a8;">',
        "Cities: " + esc(", ".join(prefs.cities)) + "<br>",
        "Edit these filters in the <code>preferences</code> table.",
        "</div></div></div>",
    ])
    return "".join(parts)


# --- sending --------------------------------------------------------------


def email_config() -> tuple[str, str, str]:
    """(api_key, from_email, my_email) from .env, or a clear DigestError."""
    load_env()
    api_key = os.getenv("RESEND_API_KEY", "").strip()
    from_email = os.getenv("FROM_EMAIL", "").strip()
    my_email = os.getenv("MY_EMAIL", "").strip()

    missing = [
        name
        for name, value in (
            ("RESEND_API_KEY", api_key),
            ("FROM_EMAIL", from_email),
            ("MY_EMAIL", my_email),
        )
        if not value
    ]
    if missing:
        raise DigestError(f"missing in .env: {', '.join(missing)}")
    if api_key.startswith("re_xxxx"):
        raise DigestError("RESEND_API_KEY is still the .env.example placeholder")

    return api_key, from_email, my_email


def assert_self_send(from_email: str, to_email: str, my_email: str) -> None:
    """Refuse, locally, the send Resend would refuse remotely.

    onboarding@resend.dev is Resend's shared sandbox sender: it works without
    verifying a domain, and in exchange Resend will only deliver from it to the
    email address that owns the API key. Any other recipient comes back as a 403
    whose message is easy to misread as an auth failure.

    Checking it here means the error names the actual constraint, and means a
    mistyped recipient never gets as far as a network call.

    CONDITIONAL ON THE SENDER SINCE PHASE 1e, and this is a narrowing rather than
    a loosening. The rule was written as "the digest may only go to MY_EMAIL",
    which was an accurate paraphrase while there was one digest and one person —
    but the reason it is true has always been the sandbox sender, and its own
    error message said so ("to send anywhere else, verify a domain in Resend and
    change FROM_EMAIL"). A fan-out makes the difference matter: with the rule
    stated as an absolute, every second user's digest is refused forever and
    verifying a domain changes nothing.

    So the guard now fires exactly when the constraint exists. FROM_EMAIL is
    still the sandbox sender on this install, so today's behaviour is unchanged
    to the character: only the MY_EMAIL account can be delivered to, and anyone
    else fails loudly before the network call and before any write. What changes
    is that verifying a domain is now sufficient to make the fan-out real.
    """
    if to_email.lower() == my_email.lower():
        return

    if from_email.lower() != SANDBOX_FROM:
        # A verified domain: Resend will deliver to anyone, so refusing here
        # would be this function inventing a restriction rather than reporting
        # one. Every recipient still comes from a public.users row — see
        # db/users.email_for_user, which raises rather than guessing.
        return

    raise DigestError(
        f"refusing to send to {to_email!r}: FROM_EMAIL is {SANDBOX_FROM}, Resend's "
        f"sandbox sender, which can only deliver to the address that owns the API "
        f"key ({my_email!r}). Verify a domain in Resend and change FROM_EMAIL to "
        f"send to anybody else — until then this account cannot receive a digest."
    )


async def send_via_resend(
    subject: str,
    html_body: str,
    text_body: str,
    *,
    api_key: str,
    from_email: str,
    to_email: str,
) -> str:
    """POST the digest to Resend. Returns the message id, or raises DigestError.

    No retry, on purpose. A retried POST after an ambiguous timeout is how one
    digest becomes two identical emails; the caller's contract is that a failure
    writes nothing, so the next scheduled run is the retry.
    """
    payload = {
        "from": from_email,
        "to": [to_email],
        "subject": subject,
        "html": html_body,
        "text": text_body,
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=SEND_TIMEOUT) as client:
        try:
            resp = await client.post(RESEND_ENDPOINT, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise DigestError(f"Resend request failed: {type(exc).__name__}: {exc}") from exc

    if resp.status_code >= 400:
        detail = resp.text.strip()
        hint = ""
        if resp.status_code == 403 and from_email.lower() == SANDBOX_FROM:
            hint = (
                f"\nHint: {SANDBOX_FROM} can only send to the address that owns the "
                f"API key. Either send to that address, or verify your own domain "
                f"in Resend and change FROM_EMAIL."
            )
        raise DigestError(f"Resend rejected the send (HTTP {resp.status_code}): {detail}{hint}")

    try:
        message_id = (resp.json() or {}).get("id")
    except ValueError as exc:
        raise DigestError(f"Resend returned a non-JSON body: {resp.text[:200]!r}") from exc

    if not message_id:
        raise DigestError(f"Resend accepted the request but returned no id: {resp.text[:200]!r}")

    return str(message_id)


# --- the run --------------------------------------------------------------


def digest_tag(today: date | None = None, suffix: str = "manual") -> str:
    """'2026-07-26-manual' — the label stamped on every notifications row."""
    return f"{(today or date.today()).isoformat()}-{suffix}"


class DigestRun:
    """What one digest run considered, matched, and sent — for ONE user."""

    def __init__(self) -> None:
        self.user_id: Any = None
        self.to: str | None = None
        self.error: str | None = None
        self.candidates: int = 0
        self.title_stage: int = 0
        self.enriched: list[dict[str, Any]] = []
        self.matched: list[Mapping[str, Any]] = []
        self.emailed: int = 0
        self.subject: str | None = None
        self.message_id: str | None = None
        self.tag: str | None = None

    @property
    def sent(self) -> bool:
        return self.message_id is not None


async def run_digest(
    conn,
    *,
    user_id=None,
    to: str | None = None,
    today: date | None = None,
    tag_suffix: str = "manual",
    sender=send_via_resend,
    describer=None,
) -> DigestRun:
    """Load, filter on titles, read descriptions, match, send, record.

    ONE USER'S DIGEST. run_fan_out below calls this once per active account; a
    caller with a user in hand can call it directly, and scripts/preview_digest.py
    still does for the one user MY_EMAIL names.

    `user_id` is whose filter, whose watch list, whose queue and whose inbox.
    It defaults to None and is then resolved from MY_EMAIL exactly once here, at
    the top of the run, rather than inside load_preferences — this is an
    entrypoint, so this is where "once per run" is a structural fact instead of a
    convention. See db/users.py: that bridge is Phase-2 scaffolding, but the
    fan-out passes an explicit id and never touches it.

    `to` defaults to THIS USER's own address, read from their row, and is checked
    against MY_EMAIL either way — the recipient is not a knob, it is an assertion
    (see assert_self_send). Until a domain is verified in Resend, that assertion
    means only the MY_EMAIL account can actually be delivered to; a second real
    user's send is refused loudly, before any network call and before any write.

    `sender` is the injection point for tests; production uses send_via_resend.
    `describer` is the same for the description fetch. Everything before and
    after the network calls is the same code either way, which is what makes "a
    failed send writes nothing" a property the tests can prove rather than one
    the tests assume.

    The enrichment sits between the two stages of the filter rather than at the
    top, so a description is fetched only for a job that has already earned one.
    """
    run = DigestRun()
    if user_id is None:
        user_id = await resolve_current_user_id(conn)
    run.user_id = user_id
    prefs = await load_preferences(conn, user_id)

    candidates = await fetch_candidates(conn, user_id)
    run.candidates = len(candidates)

    title_stage = [job for job in candidates if title_stage_ok(job, prefs)]
    run.title_stage = len(title_stage)
    run.enriched = await enrich_candidates(conn, title_stage, fetcher=describer)

    run.matched = select_matches(candidates, prefs)

    if not run.matched:
        return run

    run.subject = subject_line(run.matched, today)
    api_key, from_email, my_email = email_config()
    # The recipient is THIS USER's address, read from their own row — not
    # MY_EMAIL. Since 1e there is one digest per user, and defaulting the
    # recipient to the environment would quietly post somebody else's matches to
    # my inbox. `to` stays an override for tests only.
    to_email = (to or await email_for_user(conn, user_id)).strip()
    run.to = to_email
    assert_self_send(from_email, to_email, my_email)

    # Raises on any failure, before a single row is touched.
    run.message_id = await sender(
        run.subject,
        render_html(run.matched, prefs, today),
        render_text(run.matched, prefs, today),
        api_key=api_key,
        from_email=from_email,
        to_email=to_email,
    )

    run.tag = digest_tag(today, tag_suffix)
    run.emailed = await record_sent(
        conn, user_id, [j["canonical_id"] for j in run.matched], run.tag
    )
    return run


class FanOutRun:
    """What one fan-out considered, per user. The crawl-once half is elsewhere."""

    def __init__(self) -> None:
        self.runs: list[tuple[Any, str, DigestRun]] = []  # (user_id, email, run)

    @property
    def sent(self) -> int:
        return sum(1 for _, _, run in self.runs if run.sent)

    @property
    def emailed(self) -> int:
        return sum(run.emailed for _, _, run in self.runs)

    @property
    def failures(self) -> list[tuple[Any, str, DigestRun]]:
        return [entry for entry in self.runs if entry[2].error is not None]


async def run_fan_out(
    conn,
    *,
    today: date | None = None,
    tag_suffix: str = "manual",
    sender=send_via_resend,
    describer=None,
) -> FanOutRun:
    """One digest per user, each containing only that user's matches.

    THE OTHER HALF OF "CRAWL ONCE, FAN OUT", and the half that took until Phase
    1e because it is the one with an inbox on the end of it. The crawl is
    untouched and stays untouched: it fetches every tracked board for everybody,
    knows nothing about users, and could not narrow itself to somebody's watch
    list without making a board's history conditional on a person currently
    wanting it. All the narrowing happens here.

    Per user, in turn: their preferences, their tracked companies, their queue,
    their filter, their email, their receipts. Nothing about one user's run reads
    or writes another's — the isolation is in the SQL (every statement is bound to
    one user_id) rather than in the care taken by this loop.

    SEQUENTIAL, NOT GATHERED. The crawler holds a single asyncpg connection,
    which cannot serve concurrent queries; the same fact that makes scripts/
    crawl.py crawl companies one at a time. It also means a job whose description
    is read for the first user is already desc_checked for the second, so the
    network cost of N users is not N times the cost of one.

    ONE USER'S FAILURE IS NOT EVERYONE'S. A DigestError — a refused recipient, a
    Resend outage — is recorded against that user and the loop continues, because
    the alternative is one broken account silencing every other. Nothing is
    written for the user who failed: run_digest raises before any row is touched,
    which is the crawler's Guard 1 applied to email. The caller reports them and
    exits non-zero; a failure that only appears in a log is a failure nobody sees.

    A user with zero matches gets no email and no receipt, exactly as the single
    user always has — including never reaching email_config(), so a quiet hour
    still cannot fail on a missing secret. That is what the workflow's preflight
    secret check exists to compensate for, and it survives the fan-out unchanged.
    """
    fan_out = FanOutRun()

    for user_id, email in await digest_users(conn):
        run = DigestRun()
        run.user_id, run.to = user_id, email
        try:
            run = await run_digest(
                conn,
                user_id=user_id,
                to=email,
                today=today,
                tag_suffix=tag_suffix,
                sender=sender,
                describer=describer,
            )
        except DigestError as exc:
            # Deliberately narrow: DigestError means "the send could not be
            # attempted or was refused, and nothing was written". Anything else —
            # a broken preferences migration, a lost connection — is not
            # per-user and must not be swallowed into a report line.
            run.error = str(exc)
        fan_out.runs.append((user_id, email, run))

    return fan_out
