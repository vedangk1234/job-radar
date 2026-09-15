"""companies.tracked is written from the dashboard through ONE writer, and capped.

WHAT THIS GUARDS, AND WHY THE INVARIANT CHANGED
-----------------------------------------------
`companies.tracked` is the crawl's exclusion axis: scripts/crawl.py selects
`FROM companies WHERE tracked = TRUE`, so the column is ONE SHARED VALUE for the
whole install — turning it off stops a company being crawled for everybody.

For several phases the dashboard could not write it at all (the "sealed" era).
The crawl-selection slice REVERSED that on a deliberate, documented decision: the
operator wants to curate the crawled set from the dashboard, the app has one real
user, and the crawl needs a small enough set to finish inside the cron's timeout.
So the write is back — but funnelled through a single writer and a hard cap, the
same shape tests/test_watchlist_write.py enforces on user_companies one table over.

THE INVARIANT NOW:
  1. `lib/companies.ts` is the ONLY file under dashboard/src that writes the
     column. Every other file may read it and must not write it — a write from a
     random route or component is how the cap gets bypassed or the flag gets
     flipped by something that never went through setTracked.
  2. Inside lib/companies.ts the write lives in setTracked (the toggle) and
     upsertCompany (the Add path, TRUE-only); updateCompany must not touch it.
  3. The PATCH route ACCEPTS the field and delegates to setTracked — it must not
     silently ignore it (a caller told 200 for a write that did not happen builds
     on a lie) and must not refuse it (the field is editable now).
  4. The cap is real: setTracked names TRACKED_CAP and throws TrackedCapError.

WHY A PYTHON TEST ASSERTS ABOUT TYPESCRIPT
------------------------------------------
Because pytest is the only thing that runs on every commit. Every check inside
dashboard/ needs a live database or a running dev server; an invariant that holds
only when Supabase is reachable is an invariant nobody checks on the commit that
breaks it. `npm run ui-check` covers the rendered control; this covers the
statements behind it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD = PROJECT_ROOT / "dashboard"

# The one file allowed to write the column, and the only one. Its licence is
# setTracked (the toggle) and upsertCompany (the Add path); every other statement
# in it, and every other file, must leave the column alone.
WRITER_FILE = "src/lib/companies.ts"

# Every shape a write to this column can take, in either language.
#
# Right-anchored on word boundaries throughout, this project's own rule about
# substrings (see notify/match.py on `qa` inside "Qatar"). `\btracked` cannot
# match inside "untracked" or camelCase "hasTracked"/"setTracked", so the CSS
# class, the `.untracked-note`, the helper names and the prose are all untouched.
FORBIDDEN_WRITES = (
    r"SET\s+tracked\b",  # UPDATE companies SET tracked = …
    r"\btracked\s*=\s*",  # any bare assignment, including ON CONFLICT DO UPDATE
    r"INSERT\s+INTO\s+companies\s*\([^)]*\btracked\b",  # the insert column list
    r"\btracked\?\s*:",  # a patch type carrying it as an optional field
    r"\bpatch\.tracked\b",  # threading it through a patch object
)


def writes_the_column(code: str) -> str | None:
    """The first forbidden form present, as written. None if the file is clean."""
    for pattern in FORBIDDEN_WRITES:
        match = re.search(pattern, code)
        if match:
            return match.group(0)
    return None


def strip_comments(source: str) -> str:
    """Remove /* … */ and // … so prose about the column is not read as a write.

    Load-bearing: lib/companies.ts and the route both document the column at
    length, and this file's own reasoning names it constantly. Deliberately simple
    and safe in the direction that matters — the worst a naive stripper does is
    remove too little, which can only make this stricter. `://` is protected so a
    URL in a string is not read as a comment.
    """
    source = re.sub(r"/\*.*?\*/", " ", source, flags=re.S)
    return re.sub(r"(?<!:)//[^\n]*", " ", source)


def dashboard_sources() -> list[Path]:
    """Every TypeScript file in the app and in its check scripts."""
    files = sorted(DASHBOARD.glob("src/**/*.ts"))
    files += sorted(DASHBOARD.glob("src/**/*.tsx"))
    files += sorted(DASHBOARD.glob("scripts/*.ts"))
    return files


def relative(path: Path) -> str:
    return path.relative_to(DASHBOARD).as_posix()


@pytest.mark.parametrize("path", dashboard_sources(), ids=relative)
def test_only_the_writer_file_writes_the_tracked_column(path: Path):
    """Every file except lib/companies.ts must leave the column read-only."""
    if relative(path) == WRITER_FILE:
        pytest.skip(f"{WRITER_FILE} is the sole writer — see the test below, which pins it")

    code = strip_comments(path.read_text(encoding="utf-8"))
    found = writes_the_column(code)
    assert found is None, (
        f"{relative(path)} writes companies.tracked ({found!r}). That column is the "
        f"crawler's catalog-wide crawl flag — one shared value for every account on "
        f"this install — and it is written through lib/companies.setTracked ALONE, "
        f"which enforces the cap inside a transaction. A write anywhere else bypasses "
        f"the cap or flips the flag without going through the one gate."
    )


def test_the_writer_file_writes_tracked_only_in_setTracked_and_upsert():
    """The licence, re-earned rather than assumed.

    setTracked (the toggle) and upsertCompany (the Add path, TRUE-only) may name
    the column; updateCompany must not (it is the plain category UPDATE reachable
    from PATCH, and folding tracked into it would skip the cap transaction).
    """
    source = strip_comments((DASHBOARD / WRITER_FILE).read_text(encoding="utf-8"))

    set_tracked = re.search(r"export async function setTracked\b.*?\n}\n", source, flags=re.S)
    assert set_tracked, "setTracked has been renamed or restructured — check this assertion"
    assert writes_the_column(set_tracked.group(0)), (
        "setTracked no longer writes companies.tracked. It is the sole toggle writer; "
        "if the write moved, move this assertion with it."
    )

    upsert = re.search(r"export async function upsertCompany\b.*?\n}\n", source, flags=re.S)
    assert upsert, "upsertCompany has been renamed or restructured"
    assert writes_the_column(upsert.group(0)), "upsertCompany no longer writes the column"
    assert "tracked      = FALSE" not in upsert.group(0), (
        "the Add path may only ever set tracked TRUE — adding a company means storing "
        "it as crawled, not turning one off."
    )

    update = re.search(r"export async function updateCompany\b.*?\n}\n", source, flags=re.S)
    assert update, "updateCompany has been renamed or restructured"
    found = writes_the_column(update.group(0))
    assert found is None, (
        f"updateCompany writes companies.tracked again ({found!r}). It is the plain "
        f"category UPDATE reachable from PATCH; the tracked write belongs in "
        f"setTracked, which runs the cap check in a transaction."
    )


def test_setTracked_enforces_the_cap():
    """The cap is real, not decorative."""
    source = strip_comments((DASHBOARD / WRITER_FILE).read_text(encoding="utf-8"))
    set_tracked = re.search(r"export async function setTracked\b.*?\n}\n", source, flags=re.S)
    assert set_tracked, "setTracked has been renamed or restructured"
    body = set_tracked.group(0)
    assert "TRACKED_CAP" in body, "setTracked must compare against TRACKED_CAP"
    assert "TrackedCapError" in body, (
        "setTracked must throw TrackedCapError when the cap would be exceeded, so the "
        "transaction rolls the turn-on back"
    )


def test_the_patch_route_accepts_tracked():
    """The route accepts the field and delegates — it does not refuse or ignore it.

    Ignoring the key is worse than refusing (the caller is told 200 for a write
    that did not happen); refusing it would contradict the slice that made the
    field editable. So the route must inspect it AND hand it to setTracked.
    """
    route = DASHBOARD / "src/app/api/companies/[id]/route.ts"
    source = strip_comments(route.read_text(encoding="utf-8"))

    assert '"tracked" in body' in source, "the route no longer inspects the tracked field"
    assert "setTracked(" in source, (
        "the route must delegate the write to lib/companies.setTracked so the cap and "
        "the transaction are enforced, rather than writing the column itself"
    )
    assert "is not editable from the dashboard" not in source, (
        "the route still carries the old refusal of the tracked field — the "
        "crawl-selection slice made it editable"
    )


def test_the_column_is_still_read_and_the_toggle_is_shown():
    """Sealed no more: read, shown, AND toggleable.

    The old test asserted the control was GONE; this asserts it is BACK — a real
    crawl toggle on the company row — while the read path is intact.
    """
    companies = strip_comments((DASHBOARD / WRITER_FILE).read_text(encoding="utf-8"))
    assert "c.tracked" in companies, "companyRows() must still SELECT the column"
    assert "tracked: boolean" in companies, "CompanyRow must still carry it to the screen"

    company_list = strip_comments(
        (DASHBOARD / "src/components/CompanyList.tsx").read_text(encoding="utf-8")
    )
    assert "company.tracked" in company_list, "the card must still reflect tracked/off"
    assert "track-toggle" in company_list, (
        "the crawl toggle is gone from the company card — the crawl-selection slice "
        "put it back as an interactive control"
    )
