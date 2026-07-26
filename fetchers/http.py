"""Polite shared HTTP layer for every fetcher.

Three rules, enforced here so no individual fetcher has to remember them:

  1. One shared httpx.AsyncClient with a real User-Agent that carries a contact
     address, so a board owner who sees us in their logs can reach a human.
  2. Global concurrency cap (8) AND ~1 request/second per host. The global cap
     keeps us from opening 200 sockets; the per-host pacing is what actually
     keeps a single vendor from seeing a burst. Both are needed.
  3. Retry 429 / 5xx with exponential backoff + jitter, at most 3 attempts.
     Jitter matters: without it, several coroutines that get 429'd at the same
     moment all wake up at the same moment and 429 again.

Anything else (4xx that isn't 429, malformed JSON) is reported, not retried —
a 404 board token is an answer, not a failure.
"""

from __future__ import annotations

import asyncio
import os
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

MAX_CONCURRENCY = 8
PER_HOST_DELAY = 1.0  # seconds between two requests to the same host
MAX_ATTEMPTS = 3
BACKOFF_BASE = 1.5  # seconds; attempt N waits BACKOFF_BASE * 2**N + jitter
TIMEOUT = httpx.Timeout(30.0, connect=15.0)

_client: httpx.AsyncClient | None = None
_semaphore: asyncio.Semaphore | None = None
_host_locks: dict[str, asyncio.Lock] = {}
_host_last_hit: dict[str, float] = {}


def user_agent() -> str:
    """`job-radar/0.1 (+<MY_EMAIL>)` — the contact address comes from .env."""
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    email = os.getenv("MY_EMAIL", "").strip() or "unknown@example.com"
    return f"job-radar/0.1 (+{email})"


def get_client() -> httpx.AsyncClient:
    """The one shared client. Created lazily so it binds to the running loop."""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            headers={"User-Agent": user_agent(), "Accept": "application/json, text/html;q=0.8, */*;q=0.5"},
            timeout=TIMEOUT,
            follow_redirects=True,
            limits=httpx.Limits(max_connections=MAX_CONCURRENCY, max_keepalive_connections=MAX_CONCURRENCY),
        )
    return _client


async def close_client() -> None:
    """Close the shared client. Every script should call this before exiting."""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


def _sem() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
    return _semaphore


async def _pace(host: str, min_delay: float | None = None) -> None:
    """Hold the per-host lock until at least the required delay has passed.

    `min_delay` lets a block-sensitive vendor ask for a gentler pace than the
    global default (Workday does). It can only ever slow us down: the effective
    delay is the larger of the two, so a caller cannot use this to go faster.
    """
    delay = max(PER_HOST_DELAY, min_delay or 0.0)
    lock = _host_locks.setdefault(host, asyncio.Lock())
    async with lock:
        wait = delay - (time.monotonic() - _host_last_hit.get(host, 0.0))
        if wait > 0:
            await asyncio.sleep(wait)
        _host_last_hit[host] = time.monotonic()


def _retry_after(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("Retry-After", "").strip()
    if not raw:
        return None
    try:
        return max(0.0, min(float(raw), 30.0))  # never honour an absurd value
    except ValueError:
        return None


@dataclass
class Fetched:
    """Outcome of one polite GET. `ok` means 200 with a body we could use."""

    url: str
    status: int | None
    ok: bool
    json: Any = None
    text: str | None = None
    error: str | None = None


async def _request(
    method: str,
    url: str,
    *,
    want_json: bool,
    params: dict | None = None,
    json_body: Any = None,
    headers: dict | None = None,
    min_delay: float | None = None,
) -> Fetched:
    """One paced, retried request. The shared body of polite_get/polite_post.

    Never raises for network or HTTP reasons — the outcome is always a Fetched.
    Callers that need a failure to be loud (Workday) read `ok`/`status` and
    raise for themselves; callers that should degrade quietly return [].
    """
    host = urlsplit(url).netloc.lower()
    client = get_client()
    last_status: int | None = None
    last_error: str | None = None

    for attempt in range(MAX_ATTEMPTS):
        if attempt:
            delay = BACKOFF_BASE * (2**attempt) + random.uniform(0, 1.0)
            await asyncio.sleep(delay)

        async with _sem():
            await _pace(host, min_delay)
            try:
                resp = await client.request(
                    method, url, params=params, json=json_body, headers=headers
                )
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                last_status = None
                continue

        last_status = resp.status_code

        if resp.status_code == 429 or resp.status_code >= 500:
            last_error = f"HTTP {resp.status_code}"
            hinted = _retry_after(resp)
            if hinted:
                await asyncio.sleep(hinted)
            continue

        if resp.status_code != 200:
            return Fetched(url, resp.status_code, False, error=f"HTTP {resp.status_code}")

        if not want_json:
            return Fetched(url, 200, True, text=resp.text)

        try:
            return Fetched(url, 200, True, json=resp.json(), text=resp.text)
        except ValueError:
            return Fetched(url, 200, False, text=resp.text, error="response was not JSON")

    return Fetched(url, last_status, False, error=last_error or "exhausted retries")


async def polite_get(
    url: str,
    *,
    want_json: bool = True,
    params: dict | None = None,
    min_delay: float | None = None,
) -> Fetched:
    """GET `url` under the global semaphore and the per-host pace, with retries.

    Never raises for network or HTTP reasons — a fetcher that hits a dead board
    should degrade to "no jobs", not take the whole crawl down.
    """
    return await _request("GET", url, want_json=want_json, params=params, min_delay=min_delay)


async def polite_post(
    url: str,
    *,
    json_body: Any = None,
    headers: dict | None = None,
    want_json: bool = True,
    min_delay: float | None = None,
) -> Fetched:
    """POST a JSON body under the same semaphore, pace and retry rules as GET.

    Exists for Workday, whose board is a paginated POST search endpoint rather
    than a GET feed. Retrying a POST is safe here only because this particular
    POST is a read: it is a search query, not a mutation.
    """
    return await _request(
        "POST",
        url,
        want_json=want_json,
        json_body=json_body,
        headers=headers,
        min_delay=min_delay,
    )
