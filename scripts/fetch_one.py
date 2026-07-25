"""Fetch one board and print what the crawler would see.

    python scripts/fetch_one.py greenhouse:postman
    python scripts/fetch_one.py custom_jsonld:https://example.com/careers
    python scripts/fetch_one.py custom_sitemap:https://example.com/sitemap.xml

Prints every job the board returns — no city, keyword, seniority or date filter.
The city column is a label, not a filter: seeing "other" rows here is correct.
Writes a fixture for the raw response as a side effect.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fetchers.fixtures import FIXTURES_DIR  # noqa: E402
from fetchers.http import close_client  # noqa: E402
from fetchers.normalize import city_label  # noqa: E402
from fetchers.tier1 import TIER1_ATS, fetch  # noqa: E402
from fetchers.tier3 import jsonld_jobpostings, sitemap_jobs  # noqa: E402

USAGE = (
    "usage: python scripts/fetch_one.py <ats:token>\n"
    f"  tier 1: {', '.join(TIER1_ATS)}  (token = board token)\n"
    "  tier 3: custom_jsonld, custom_sitemap  (token = full URL)"
)


def _truncate(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


async def main(spec: str) -> int:
    if ":" not in spec:
        print(USAGE, file=sys.stderr)
        return 2

    ats, token = spec.split(":", 1)
    ats, token = ats.strip().lower(), token.strip()
    if not token:
        print(USAGE, file=sys.stderr)
        return 2

    before = set(FIXTURES_DIR.glob("*.json")) if FIXTURES_DIR.exists() else set()

    try:
        if ats in TIER1_ATS:
            jobs = await fetch(ats, token)
        elif ats == "custom_jsonld":
            jobs = await jsonld_jobpostings(token)
        elif ats == "custom_sitemap":
            jobs = await sitemap_jobs(token)
        else:
            print(f"unknown ats {ats!r}\n\n{USAGE}", file=sys.stderr)
            return 2
    finally:
        await close_client()

    print(f"{ats}:{token} -> {len(jobs)} job(s)\n")
    if not jobs:
        print("(board returned nothing — bad token, empty board, or a vendor error)")
        return 1

    header = f"{'TITLE':<58} {'CITY':<14} {'LOCATION':<34} URL"
    print(header)
    print("-" * len(header))

    counts: dict[str, int] = {}
    for job in jobs:
        label = city_label(job["location"])
        counts[label] = counts.get(label, 0) + 1
        print(
            f"{_truncate(job['title'] or '(no title)', 58):<58} "
            f"{label:<14} "
            f"{_truncate(job['location'] or '(none)', 34):<34} "
            f"{job['url'] or '(no url)'}"
        )

    print("\ncity labels: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    after = set(FIXTURES_DIR.glob("*.json")) if FIXTURES_DIR.exists() else set()
    for path in sorted(after - before):
        print(f"fixture: {path.relative_to(FIXTURES_DIR.parent)}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(USAGE, file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(asyncio.run(main(sys.argv[1])))
