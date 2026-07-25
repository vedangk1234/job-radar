"""Render the matched jobs into one email, and send it through Resend.

One email, never several. A digest that arrives as eleven separate messages is
the same failure as an inbox full of noise — you stop opening it — so the run
either produces a single grouped message or produces nothing at all.

And *nothing at all* is a real outcome here, not an error. Zero matches sends
zero emails. An empty digest that shows up every morning saying "no new jobs"
trains you to ignore the sender, which means the one morning it is not empty you
ignore that too.

Ordering, which is the safety property
--------------------------------------
    render -> send -> (Resend returns an id) -> flip notified, write the receipt

The write happens last and only on success. If Resend errors, times out, or
rejects the recipient, this module raises and touches no rows: every job stays
notified=FALSE and the next run picks up exactly the same set. That is the
crawler's Guard 1 philosophy applied to email — when the outcome is in doubt,
change nothing.

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

from .match import Preferences, select_matches
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
    """
    if to_email.lower() == my_email.lower():
        return

    detail = (
        f"\n{SANDBOX_FROM} is Resend's sandbox sender: it can only deliver to the "
        f"address that owns the API key. To send anywhere else, verify a domain in "
        f"Resend and change FROM_EMAIL."
        if from_email.lower() == SANDBOX_FROM
        else ""
    )
    raise DigestError(
        f"refusing to send to {to_email!r}: the digest may only go to MY_EMAIL "
        f"({my_email!r}).{detail}"
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
    """What one digest run considered, matched, and sent."""

    def __init__(self) -> None:
        self.candidates: int = 0
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
    to: str | None = None,
    today: date | None = None,
    tag_suffix: str = "manual",
    sender=send_via_resend,
) -> DigestRun:
    """Load, match, send, record. The entire Step 4 pipeline in one call.

    `to` defaults to MY_EMAIL and is checked against it either way — the
    recipient is not a knob, it is an assertion (see assert_self_send).

    `sender` is the injection point for tests; production uses send_via_resend.
    Everything before and after the send is the same code either way, which is
    what makes "a failed send writes nothing" a property the tests can prove
    rather than one the tests assume.
    """
    run = DigestRun()
    prefs = await load_preferences(conn)

    candidates = await fetch_candidates(conn)
    run.candidates = len(candidates)
    run.matched = select_matches(candidates, prefs)

    if not run.matched:
        return run

    run.subject = subject_line(run.matched, today)
    api_key, from_email, my_email = email_config()
    to_email = (to or my_email).strip()
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
    run.emailed = await record_sent(conn, [j["canonical_id"] for j in run.matched], run.tag)
    return run
