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

from typing import Callable, Coroutine

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
