"""The dashboard cannot switch the crawler off — companies.tracked is read, never written.

WHAT THIS SEALS, AND WHY IT WAS A HOLE RATHER THAN A ROUGH EDGE
---------------------------------------------------------------
`companies.tracked` is the crawl's exclusion axis: scripts/crawl.py selects
`FROM companies WHERE tracked = TRUE`, so the column is ONE SHARED VALUE for the
whole install. It is not a preference, it has no owner, and nothing about it is
per-user — the per-user thing is `user_companies`, a different table on a
different axis, which no route writes yet.

Until this commit every company row on Home and Jobs carried a checkbox that
PATCHed it. The route behind that checkbox gates on lib/guard.requireSession(),
which asks whether there is a session and never whose, and the UPDATE was scoped
by company id alone. With sign-up open since Phase 2a that composed into: any
account, ninety seconds old, could set `tracked = FALSE` on any company — or on
all fifty — and every other account's hourly crawl and digest would go quiet,
with nothing anywhere recording who did it or why. One user could silence
everybody's inbox from a UI control that looked like a personal setting.

THE FIX IS A SUBTRACTION, WHICH IS WHY A TEXTUAL TEST CAN GUARD IT
------------------------------------------------------------------
The write was deleted rather than gated. Gating needs an operator identity and
there is deliberately none in the deployed app — lib/env.ts has carried no
myEmail() since Phase 2b, and the comment there is explicit that an ambient
identity read in src/ becomes the thing the next page in a hurry reaches for. So
a gate would have meant reversing 2b in order to defend a column the web app has
no business owning. A statement that does not exist cannot be reached by curl,
by a forgotten route, or by a condition someone later edits the wrong way round.

That is what makes the invariant textual and cheap: the guarantee is "no
statement under dashboard/src writes this column", and a name absent from the
source cannot be executed. Same shape as tests/test_user_companies.py (the crawl
path has never heard of the watch list) and tests/test_dashboard_notified.py (the
dashboard has never heard of the dropped boolean), pointed at a column that still
exists and must stay readable.

WHY A PYTHON TEST ASSERTS ABOUT TYPESCRIPT
------------------------------------------
Because pytest is the only thing that runs on every commit. Every check inside
dashboard/ needs something this does not: a live database (`db:check`,
`badge-check`, `match-parity`) or a running dev server (`ui-check`). An invariant
that holds only when Supabase is reachable is an invariant nobody checks on the
commit that breaks it. `npm run ui-check` covers the half this cannot see — that
the control is really gone from the rendered markup — and this covers the half
ui-check cannot reach, which is every statement behind it.

THE ONE EXEMPTION, AND WHY IT IS EARNED RATHER THAN GRANTED
-----------------------------------------------------------
lib/companies.upsertCompany — the Add screen's write — still names the column,
because adding a company to the catalog means storing it as tracked. It can only
ever set TRUE, so it cannot stop a crawl and is not the hole this file closes.
It is a smaller, separate surface: a signed-in stranger re-adding an existing
company by name flips a deliberately-off row back on and overwrites its `name`.
That is on the deferred list, not fixed here.

The exemption is therefore one FILE, pinned by one TEST: the file is allowed to
name the column only inside upsertCompany, and test_the_exempt_file_writes_only
_the_add_path asserts exactly that. An exemption nobody re-earns is how an
allowlist quietly becomes a hole — the shape tests/test_dashboard_notified.py
describes from the other end, where the one exemption it ever granted was
enumerated, guarded by its own test, and deleted the moment it stopped being
earned.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD = PROJECT_ROOT / "dashboard"

# The file allowed to name the column in a write, and the only one. Its licence
# is upsertCompany and nothing else; the test below reads the file apart and
# proves the other statement in it stays clean.
ADD_PATH_FILE = "src/lib/companies.ts"

# Every shape a write to this column can take, in either language.
#
# The SQL forms are the ones that actually reach Postgres. The two TypeScript
# forms are the plumbing that would have to exist to feed them, and they are
# listed because catching the wiring on the commit that adds it is cheaper than
# catching the statement it eventually grows: `tracked?:` is the optional-field
# spelling a patch type uses, and it cannot collide with the READ path, where the
# column is a required `tracked: boolean` on CompanyRow.
#
# Right-anchored on word boundaries throughout, this project's own rule about
# substrings (see notify/match.py on `qa` inside "Qatar"). `\btracked` cannot
# match inside "untracked", so the CSS class, the `.untracked-note` and the prose
# about untracked companies are all untouched by it.
FORBIDDEN_WRITES = (
    r"SET\s+tracked\b",  # UPDATE companies SET tracked = …
    r"\btracked\s*=\s*",  # any assignment, including ON CONFLICT DO UPDATE
    r"INSERT\s+INTO\s+companies\s*\([^)]*\btracked\b",  # the insert column list
    r"\btracked\?\s*:",  # a patch type carrying it as an optional field
    r"\bpatch\.tracked\b",  # threading it through the patch object
)


def writes_the_column(code: str) -> str | None:
    """The first forbidden form present, as written. None if the file is clean."""
    for pattern in FORBIDDEN_WRITES:
        match = re.search(pattern, code)
        if match:
            return match.group(0)
    return None


def strip_comments(source: str) -> str:
    """Remove /* … */ and // … so prose about the column is not a write of it.

    Load-bearing here rather than incidental: the whole point of the fix is
    documented in comments that quote the deleted statement — lib/companies.ts
    prints the old `tracked = COALESCE(...)` line, the route explains what
    `WHERE tracked = TRUE` means to the crawler, and this file's own reasoning
    would be unreadable if it had to talk around its subject. A rule that forced
    the documentation to avoid naming the thing it documents is a rule that makes
    the code worse.

    Deliberately simple, and safe in the direction that matters: the worst a
    naive stripper does is remove too little, which can only make this stricter.
    `://` is protected so a URL in a string is not read as a comment.
    """
    source = re.sub(r"/\*.*?\*/", " ", source, flags=re.S)
    return re.sub(r"(?<!:)//[^\n]*", " ", source)


def dashboard_sources() -> list[Path]:
    """Every TypeScript file in the app and in its check scripts.

    Globbed rather than hand-listed: the rule is "nothing in the dashboard", so a
    route or component added next month has to be covered without anybody
    remembering this file exists. The check scripts are included because they run
    against the live database with the same credentials the app has.
    """
    files = sorted(DASHBOARD.glob("src/**/*.ts"))
    files += sorted(DASHBOARD.glob("src/**/*.tsx"))
    files += sorted(DASHBOARD.glob("scripts/*.ts"))
    return files


def relative(path: Path) -> str:
    return path.relative_to(DASHBOARD).as_posix()


@pytest.mark.parametrize("path", dashboard_sources(), ids=relative)
def test_no_dashboard_file_writes_the_tracked_column(path: Path):
    """The seal itself, over every file, with one enumerated exemption."""
    if relative(path) == ADD_PATH_FILE:
        pytest.skip(f"{ADD_PATH_FILE} is the Add upsert — see the test below, which pins it")

    code = strip_comments(path.read_text(encoding="utf-8"))
    found = writes_the_column(code)
    assert found is None, (
        f"{relative(path)} writes companies.tracked ({found!r}). That column is the "
        f"crawler's catalog-wide crawl flag — one shared value for every account on "
        f"this install — so a signed-in request must not be able to change it; a "
        f"single user could stop the crawl for everybody. If what you want is "
        f"'this person wants alerts for this company', that is user_companies, a "
        f"different table on a different axis."
    )


def test_the_exempt_file_writes_only_the_add_path():
    """The exemption, re-earned rather than assumed.

    Splits lib/companies.ts at its two statements and asks a different question of
    each: upsertCompany may name the column (adding a company means storing it as
    tracked, and it can only ever set TRUE), updateCompany may not name it at all.

    Without this, ADD_PATH_FILE above would be a hole big enough to drive the
    original bug back through — the deleted UPDATE lived in this very file, four
    functions below the one that is allowed to stay.
    """
    source = strip_comments((DASHBOARD / ADD_PATH_FILE).read_text(encoding="utf-8"))

    update = re.search(r"export async function updateCompany\b.*?\n}\n", source, flags=re.S)
    assert update, "updateCompany has been renamed or restructured — check this assertion still holds"
    found = writes_the_column(update.group(0))
    assert found is None, (
        f"lib/companies.updateCompany writes companies.tracked again ({found!r}). "
        f"That is the statement the security fix removed: it is reachable from "
        f"PATCH /api/companies/:id, whose only gate is 'is there a session'."
    )

    upsert = re.search(r"export async function upsertCompany\b.*?\n}\n", source, flags=re.S)
    assert upsert, "upsertCompany has been renamed or restructured"
    assert writes_the_column(upsert.group(0)), (
        "upsertCompany no longer writes companies.tracked, so the exemption above "
        "is no longer earned. Delete ADD_PATH_FILE from this file — an exemption "
        "kept past its reason is just a hole with a comment on it."
    )
    assert "tracked      = FALSE" not in upsert.group(0), (
        "the Add path may only ever set tracked TRUE. Setting it FALSE from a "
        "signed-in request is the hole this file exists to keep shut."
    )


def test_the_patch_route_refuses_the_field_explicitly():
    """The positive half of the route change.

    Absence alone is satisfied by a route that silently IGNORES `tracked`, which
    is worse than the bug in one specific way: the caller is told 200 for a write
    that did not happen, and the next person reasonably concludes the flag is
    per-user and builds on it. So the refusal is asserted to exist, and to be a
    refusal rather than a shrug.
    """
    route = DASHBOARD / "src/app/api/companies/[id]/route.ts"
    source = strip_comments(route.read_text(encoding="utf-8"))

    assert '"tracked" in body' in source, (
        "the route no longer inspects the tracked field at all. It must REFUSE it, "
        "not ignore it — an ignored key reads to the caller as a successful write."
    )
    refusal = re.search(r'if \("tracked" in body\) \{(.*?)\n  \}', source, flags=re.S)
    assert refusal, "the tracked branch has been restructured — check this assertion still holds"
    assert "badRequest" in refusal.group(1), (
        "the tracked branch must return badRequest. 400 rather than 403 on purpose: "
        "403 implies somebody may, and nobody may — the statement is gone."
    )


def test_the_column_is_still_read_and_still_shown():
    """The other positive half: sealed, not deleted.

    Every assertion above is satisfied by removing the column from the dashboard
    entirely — no SELECT, no pill, nothing on screen. That would pass this file
    and lose something real: whether a company is being crawled explains why its
    jobs are stale, and CompanyList prints a note saying exactly that. Reading a
    crawler fact is not owning it, and this is what keeps the two apart.
    """
    companies = strip_comments((DASHBOARD / ADD_PATH_FILE).read_text(encoding="utf-8"))
    assert "c.tracked" in companies, "companyRows() must still SELECT the column"
    assert "tracked: boolean" in companies, "CompanyRow must still carry it to the screen"

    company_list = strip_comments(
        (DASHBOARD / "src/components/CompanyList.tsx").read_text(encoding="utf-8")
    )
    assert "company.tracked" in company_list, "the row must still show tracked/off"

    # And it must be shown by something inert. The control that used to be here
    # was a checkbox inside a <label className="switch">, so both halves are named
    # — a re-added input under a different class name would still be caught by the
    # forbidden-writes rule the moment it was wired to anything.
    assert 'className="switch"' not in company_list, (
        "the tracked pill is back inside the switch label that carried the checkbox"
    )
    assert "aria-label={`Track " not in company_list, (
        "a `Track <company>` control is back on the company row"
    )
