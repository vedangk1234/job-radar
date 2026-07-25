"""Raw-response archive.

Every successful board response is written to disk verbatim, before any
normalisation touches it. That archive is what makes the later steps testable
offline and what settles the "did the vendor change, or did we break it?"
question when a mapping suddenly returns None.

Files land in job-radar/fixtures/, which is gitignored: they are regenerated on
every crawl and would otherwise bloat the repo.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _slug(value: Any) -> str:
    """Make a token safe for a filename without losing its identity."""
    text = _UNSAFE.sub("-", str(value or "unknown")).strip("-")
    return (text or "unknown")[:80]


def save_fixture(ats: str, token: str, raw_json: Any, *, suffix: str = "") -> Path:
    """Write one raw response to fixtures/{ats}_{token}_{timestamp}.json.

    `suffix` distinguishes the pages of a paginated board (SmartRecruiters), so
    page 2 does not overwrite page 1 when both land in the same second.
    Returns the path written.
    """
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    extra = f"_{_slug(suffix)}" if suffix else ""
    path = FIXTURES_DIR / f"{_slug(ats)}_{_slug(token)}_{stamp}{extra}.json"

    # A same-second collision (two pages, no suffix) would silently lose data.
    counter = 1
    while path.exists():
        path = FIXTURES_DIR / f"{_slug(ats)}_{_slug(token)}_{stamp}{extra}-{counter}.json"
        counter += 1

    with path.open("w", encoding="utf-8") as handle:
        json.dump(raw_json, handle, ensure_ascii=False, indent=1)
    return path
