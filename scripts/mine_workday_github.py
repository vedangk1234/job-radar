"""Mine Workday careers URLs out of GitHub job-list READMEs into discovered_companies.

    python scripts/mine_workday_github.py             # mine and store
    python scripts/mine_workday_github.py --dry-run   # mine and report, write nothing
    python scripts/mine_workday_github.py --repos-per-host 20

The GitHub miner in fetchers/discovery.py has always read these READMEs for
Greenhouse/Lever/Ashby tokens and treated a Workday link as a name-only lead —
a tenant with a `"try Workday"` note for a human to finish by hand. That was
right when all the miner looked at was the HOST, because a tenant is one of
three unguessable strings and the other two are not in a hostname.

But the READMEs do not only write hostnames. They write whole careers URLs, and
a whole URL carries the site:

    https://genpact.wd108.myworkdayjobs.com/en-US/External_Careers
            ^tenant  ^pod                     ^lang ^site

That is a complete config, not a lead. So this script reads the same READMEs the
sweep reads, pulls the full URLs out, and hands each one to the SAME
fetchers.resolve.parse_workday_url that scripts/add_workday.py uses — no second
parser, because two parsers would drift and the crawler only obeys one of them.

ALL THREE PIECES OR NOTHING
---------------------------
A URL that does not yield host AND tenant AND site — or that parses with
`ambiguous` set, which is parse_workday_url's own way of saying "I guessed" — is
counted and DROPPED. This is the same refusal add_workday.py makes and for the
same reason: a Workday row missing a piece is not a company that might work, it
is a fetch that fails every 30 minutes forever. A skip is a result, not a loss.

WHAT THIS SCRIPT MAY WRITE
--------------------------
`discovered_companies`, and nothing else. `MINE_SQL` below is the only statement
here that writes anything and it names exactly one table;
tests/test_workday_mining.py asserts that against the statement TEXT rather than
against this comment, the same way tests/test_commoncrawl.py seals the harvester.

Nothing is promoted, tracked or crawled. A row written here has NULL job_count
and NULL city_hits because no one has fetched the board — that is a job for
scripts/add_workday.py or a later verify pass, and the point of this script is
to tell a person whether the queue is worth working at all.

PACING AND RESUMING
-------------------
Requests go through fetchers/discovery.py unchanged, which means the GitHub
search API keeps its explicit 6.5s pace (the unauthenticated budget is ten a
minute) and the raw CDN keeps the shared 1/second. GITHUB_TOKEN is used if set
and never required.

Rows are written per README rather than at the end, and every (ats, token)
already in `companies` or `discovered_companies` is skipped without a write — so
an interrupted run resumes by simply being run again.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crawler.discover import load_known, looks_like_services  # noqa: E402
from db.connection import get_crawler_conn  # noqa: E402
from fetchers.discovery import (  # noqa: E402
    candidate_repos,
    extract_workday_urls,
    fetch_readme,
    github_token,
)
from fetchers.http import close_client  # noqa: E402
from fetchers.normalize import norm  # noqa: E402
from fetchers.resolve import parse_workday_url  # noqa: E402

# The only writing statement in this module.
#
# ON CONFLICT DO NOTHING, never DO UPDATE — deliberately not the DO UPDATE that
# crawler/discover.LOG_SQL uses. A mined row knows strictly LESS than a probed
# one: no job count, no city hits, no verdict, and a config nobody has fetched
# against. If (workday, tenant) is already in this table because somebody
# verified it by hand, this statement must leave that row completely alone.
#
# job_count, city_hits and last_posting_at are omitted rather than zeroed. They
# stay NULL, which reads as "no one has looked at this board yet" — true, and
# distinguishable from a board that was fetched and found empty.
MINE_SQL = """
INSERT INTO discovered_companies (name, ats, token, config,
                                  looks_like_services, source)
VALUES ($1, 'workday', $2, $3::jsonb, $4, 'github-workday')
ON CONFLICT (ats, token) DO NOTHING
"""

SOURCE = "github-workday"

# The keys a Workday config must carry for the fetcher to work, and the exact
# shape scripts/add_workday.py stores. `tenant` is not among them because it is
# the `token` column, not config — same as a Greenhouse board's token.
CONFIG_KEYS = ("host", "site", "lang", "careers_url")


@dataclass
class Stats:
    """The tally the whole run exists to produce."""

    repos_found: int = 0
    readmes_read: int = 0
    urls_found: int = 0  # every myworkdayjobs.com URL seen, before any judgement
    urls_distinct: int = 0  # after deduping the same URL written in two READMEs
    parsed: int = 0  # yielded host + tenant + site, unambiguously
    skipped: int = 0  # partial or ambiguous — dropped on purpose
    known: int = 0  # (workday, tenant) already a board, or already tracked
    name_collision: int = 0  # tenant matches a harvested token name; counted, not acted on
    duplicate_tenant: int = 0  # two URLs, one tenant, within this run
    stored: int = 0
    skips: list[tuple[str, str]] = field(default_factory=list)
    added: list[tuple[str, str]] = field(default_factory=list)  # (tenant, site)


def mine_row(name: str, tenant: str, config: dict) -> tuple:
    """The discovered_companies parameter tuple for one parsed Workday URL.

    `name` is the tenant, which is all this source knows — the same choice
    fetchers/discovery.py makes for a board lifted out of a README and
    scripts/harvest_commoncrawl.py makes for a harvested token. A person reading
    the queue renames it when they verify it.
    """
    return (name, tenant, json.dumps(config), looks_like_services(name))


def config_from(parsed: dict, repo: str) -> dict:
    """The stored config: add_workday.py's four keys, plus where it came from.

    The four keys are copied rather than the whole parse result so the row holds
    what fetchers/workday.py reads and nothing that only made sense during
    parsing. `mined_from` is provenance — the README that named this URL — so a
    person working the queue can see the claim's source before trusting it.
    """
    config = {key: parsed[key] for key in CONFIG_KEYS if parsed.get(key)}
    config["mined_from"] = f"github:{repo}"
    return config


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--repos-per-host", type=int, default=12, metavar="N",
        help="repositories to take from each hostname search (default 12)",
    )
    parser.add_argument(
        "--max-repos", type=int, default=None, metavar="N",
        help="stop after reading N READMEs (default: all of them)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="mine and report, but write nothing to discovered_companies",
    )
    return parser.parse_args(argv)


def print_report(stats: Stats, *, dry_run: bool) -> None:
    print("\n=== Workday mining report ===")
    print(f"  repositories found by search : {stats.repos_found}")
    print(f"  READMEs actually read        : {stats.readmes_read}")
    print(f"  myworkdayjobs.com URLs seen  : {stats.urls_found} "
          f"({stats.urls_distinct} distinct)")
    print(f"  parsed to tenant/pod/site    : {stats.parsed}")
    print(f"  skipped (partial/ambiguous)  : {stats.skipped}")
    print(f"  already known (deduped)      : {stats.known}")
    print(f"  name collides with a harvest row: {stats.name_collision} "
          f"(counted, not skipped — see the dedupe note)")
    print(f"  same tenant twice in this run: {stats.duplicate_tenant}")
    print(f"  NEW rows in discovered_companies: {stats.stored}")

    if stats.skips:
        print(f"\nskipped URLs ({len(stats.skips)}) — a partial Workday config is a "
              f"crawl that fails forever, so these are dropped:")
        for url, why in stats.skips[:20]:
            print(f"  {why:<28} {url}")
        if len(stats.skips) > 20:
            print(f"  … and {len(stats.skips) - 20} more")

    if stats.added:
        print(f"\nnewly added ({len(stats.added)}) — tenant and site, for eyeballing:")
        print(f"  {'TENANT':<28} SITE")
        for tenant, site in stats.added[:40]:
            print(f"  {tenant:<28} {site}")
        if len(stats.added) > 40:
            print(f"  … and {len(stats.added) - 40} more")
    else:
        print("\nnewly added: none")

    if dry_run:
        print("\n--dry-run: nothing was written to discovered_companies.")


async def print_totals(conn) -> None:
    discovered = await conn.fetchval("SELECT count(*) FROM discovered_companies")
    mined = await conn.fetchval(
        "SELECT count(*) FROM discovered_companies WHERE source = $1", SOURCE
    )
    workday = await conn.fetchval(
        "SELECT count(*) FROM discovered_companies WHERE ats = 'workday'"
    )
    companies = await conn.fetchval("SELECT count(*) FROM companies")
    jobs = await conn.fetchval("SELECT count(*) FROM jobs")
    print(f"\ndiscovered_companies: {discovered} row(s), {workday} with ats=workday, "
          f"{mined} from {SOURCE}")
    print(f"companies table: {companies} row(s) — untouched by this script, by design")
    print(f"jobs table: {jobs} row(s) — untouched by this script, by design")


async def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    auth = "with GITHUB_TOKEN" if github_token() else "unauthenticated"
    print(f"mining github for Workday careers URLs ({auth}) …")

    conn = await get_crawler_conn()
    stats = Stats()
    seen_urls: set[str] = set()
    seen_tenants: set[str] = set()

    try:
        known = await load_known(conn)
        known_boards = known.company_boards | known.logged_boards
        workday_boards = sum(1 for ats, _ in known_boards if ats == "workday")
        print(f"{len(known_boards)} board(s) already known, {workday_boards} of them "
              f"Workday — those are skipped")

        repos = await candidate_repos(repos_per_host=args.repos_per_host)
        if args.max_repos is not None:
            repos = repos[: args.max_repos]
        stats.repos_found = len(repos)
        print(f"{len(repos)} repository/ies to read\n")

        for index, repo in enumerate(repos, start=1):
            text = await fetch_readme(repo)
            if not text:
                print(f"  … {index}/{len(repos)} {repo} — no README", flush=True)
                continue
            stats.readmes_read += 1

            rows: list[tuple] = []
            urls = extract_workday_urls(text)
            stats.urls_found += len(urls)

            for url in urls:
                key = url.lower()
                if key in seen_urls:
                    continue
                seen_urls.add(key)
                stats.urls_distinct += 1

                parsed = parse_workday_url(url)
                tenant = (parsed.get("tenant") or "").strip()
                site = (parsed.get("site") or "").strip()
                host = (parsed.get("host") or "").strip()

                # All three or nothing. `ambiguous` is parse_workday_url saying
                # it guessed, which on a vanity domain means the tenant is very
                # likely wrong — store that and the crawl fails forever.
                if parsed.get("ambiguous") or not (tenant and site and host):
                    stats.skipped += 1
                    missing = parsed.get("ambiguous") or [
                        piece for piece, value in
                        (("host", host), ("tenant", tenant), ("site", site))
                        if not value
                    ]
                    stats.skips.append((url, "missing " + ",".join(missing)))
                    continue
                stats.parsed += 1

                if tenant.lower() in seen_tenants:
                    stats.duplicate_tenant += 1
                    continue
                seen_tenants.add(tenant.lower())

                # Already known, on the two questions that actually mean it:
                # is (workday, tenant) already a board in either table, and is
                # this tenant already a company somebody tracks?
                #
                # NOT crawler.discover.Known.seen, and the difference matters
                # here in a way it does not for the sweep. `seen` also matches
                # the normalised NAME against discovered_companies, which since
                # the Common Crawl harvest holds ~11.5k rows whose name IS a
                # Greenhouse/Lever token. A Workday tenant colliding with one of
                # those — `redfin`, `imc` — says nothing about whether anyone
                # knows this company's Workday config, and suppressing it would
                # make this miner report zero for the wrong reason. The board
                # pair is the precise question; the tracked-company names are
                # 50 rows a person curated, so those still count.
                key_tenant = norm(tenant)
                if ("workday", tenant) in known_boards or key_tenant in known.company_names:
                    stats.known += 1
                    continue
                if key_tenant in known.logged_names:
                    # Worth counting out loud rather than acting on: it is a
                    # name collision with the harvest, not a known Workday row.
                    stats.name_collision += 1

                rows.append(mine_row(tenant, tenant, config_from(parsed, repo)))
                stats.added.append((tenant, site))

            # Written per README rather than at the end, so an interrupted run
            # leaves behind what it had already learned and the next run resumes
            # against it.
            if rows and not args.dry_run:
                await conn.executemany(MINE_SQL, rows)
            stats.stored += len(rows)

            print(
                f"  … {index}/{len(repos)} {repo} — "
                f"{len(urls)} workday url(s), {len(rows)} new",
                flush=True,
            )

        print_report(stats, dry_run=args.dry_run)
        await print_totals(conn)
    finally:
        await close_client()
        await conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
