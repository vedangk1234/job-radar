"""The dashboard no longer reads jobs.notified — the mirror of the crawl-path invariant.

Phase 1d moved "this person has been emailed about this job" into
user_job_notifications and 1e moved the digest queue onto it, which left exactly
one reader of the old global boolean: the dashboard's NEW / NOTIFIED badge. Phase
1f moved that too, and Phase 1g drops the column.

This file is what makes the drop a no-op that cannot surprise anybody. It is the
same shape as the assertions in tests/test_user_companies.py and
tests/test_user_notifications.py — a name absent from the source cannot be read,
joined, or reintroduced by an edit that looked local — pointed at the other
runtime.

WHY A PYTHON TEST ASSERTS ABOUT TYPESCRIPT
------------------------------------------
Because the invariant is textual and pytest is the only thing that runs on every
commit. The dashboard's own checks each need something this does not: a live
database (`db:check`, `match-parity`, `badge-check`) or a running dev server
(`ui-check`). An invariant that only holds when Supabase is reachable is an
invariant nobody checks on the commit that breaks it.

WHAT IS FORBIDDEN, AND WHY IT IS THE PUNCTUATION RATHER THAN THE WORD
---------------------------------------------------------------------
There are exactly two ways to read that column: name it in SQL (`j.notified`,
`jobs.notified`) or read it off a row object in TypeScript (`job.notified`). All
three end in `.notified`, and that is what is banned.

The bare WORD "notified" is deliberately still allowed, because it is still the
right word for several things that are not the column: the table
`user_job_notifications`, the derived per-user field `user_notified`, the
timestamp `notified_at`, the CSS class `badge notified`, and the text a job row
prints. The rename in Phase 1f — `notified` -> `user_notified` on JobRow — is
what makes this precision possible: no per-user field starts with `notified`, so
banning the dotted form needs no allowlist of near-misses to maintain.

Comments are stripped before the check. The prose in lib/badges.ts and
lib/jobs.ts explains what `jobs.notified` was and why the badge stopped reading
it, and naming the column is the clearest way to say that. A rule that forced the
documentation to talk around the thing it is documenting would be a rule that
makes the code worse.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD = PROJECT_ROOT / "dashboard"

# The two shapes a read can take, and the one that subsumes them. Listed in full
# rather than collapsed to `.notified` alone so the failure message names the
# thing that was actually written.
FORBIDDEN = ("j.notified", "jobs.notified", ".notified")

# THE SINGLE EXEMPTION, AND IT EXPIRES IN 1g.
#
# scripts/badge-check.ts is the migration's own before/after comparison: it reads
# both the retiring boolean and the per-user table for the same user and fails if
# they disagree about one job. It has to name both sides to compare them. It is
# not part of the app, renders no page and runs no route — it is a check a person
# runs by hand against the live database.
#
# When 1g drops the column this section of that script stops being possible and
# stops being needed, so 1g's diff deletes it and this exemption with it. Until
# then the exemption is enumerated here rather than being a hole: the test below
# asserts the file still reads the column, so an exemption that stopped being
# earned fails instead of quietly widening what is allowed.
EXEMPT = {"scripts/badge-check.ts"}


def dashboard_sources() -> list[Path]:
    """Every TypeScript file in the app and its check scripts.

    Globbed rather than hand-listed, unlike CRAWL_PATH_FILES in
    tests/test_user_companies.py. That list is hand-made because "the crawl path"
    is a judgment about which modules a crawl executes, and a wildcard would
    absorb a new one silently. This is the opposite kind of set: the rule is
    "nothing in the dashboard", so a file added tomorrow must be covered without
    anybody remembering to add it here.
    """
    files = sorted(DASHBOARD.glob("src/**/*.ts"))
    files += sorted(DASHBOARD.glob("src/**/*.tsx"))
    files += sorted(DASHBOARD.glob("scripts/*.ts"))
    return files


def strip_comments(source: str) -> str:
    """Remove /* … */ and // … so prose about the column is not a read of it.

    Deliberately simple, and safe in the direction that matters: the worst a
    naive stripper can do here is remove too little, which can only make this
    test stricter. `://` is protected so a URL inside a string is not read as the
    start of a comment.
    """
    source = re.sub(r"/\*.*?\*/", " ", source, flags=re.S)
    return re.sub(r"(?<!:)//[^\n]*", " ", source)


def relative(path: Path) -> str:
    return path.relative_to(DASHBOARD).as_posix()


@pytest.mark.parametrize(
    "path", [p for p in dashboard_sources() if relative(p) not in EXEMPT], ids=relative
)
def test_no_dashboard_file_reads_the_notified_column(path: Path):
    """The precondition Phase 1g depends on, as a fact about the source.

    If this passes, dropping jobs.notified cannot change what any screen shows,
    because no screen consults it. That is the same argument
    tests/test_user_companies.py makes about the crawl never having heard of the
    watch list, applied to a column instead of a table.
    """
    code = strip_comments(path.read_text(encoding="utf-8"))
    for forbidden in FORBIDDEN:
        assert forbidden not in code, (
            f"{relative(path)} reads the retiring jobs.notified column ({forbidden!r}). "
            f"Per-user notification state is a row in user_job_notifications — see "
            f"lib/jobs.notifiedToUser."
        )


def test_the_badge_reads_the_per_user_table_instead():
    """The positive half. Absence alone would also be satisfied by no badge at all.

    A file that stopped reading the boolean and started reading nothing would
    pass every assertion above while rendering NEW on every row forever — wrong,
    silent, and in the direction that says "nobody has emailed you about any of
    this". So the replacement is asserted to exist, and to be scoped by user.
    """
    source = (DASHBOARD / "src/lib/jobs.ts").read_text(encoding="utf-8")

    assert "FROM user_job_notifications n" in source  # the per-user table
    assert "n.user_id = ${userId}" in source  # scoped to somebody
    assert "AS user_notified" in source  # under the name the badge reads

    badges = strip_comments((DASHBOARD / "src/lib/badges.ts").read_text(encoding="utf-8"))
    assert "user_notified" in badges, "homeBadge must derive NEW/NOTIFIED from the per-user fact"


def test_the_shared_column_list_carries_no_notified_column():
    """lib/jobs.jobColumns() is what puts columns on every row, so it is the choke point.

    The boolean travelled to all three queries — both job lists and the applied
    write's RETURNING — from this one fragment. Asserting on the fragment itself
    catches a re-add at the only place it could be made once and take effect
    everywhere.
    """
    source = (DASHBOARD / "src/lib/jobs.ts").read_text(encoding="utf-8")
    match = re.search(r"const jobColumns = \(\) => sql`(.*?)`", source, flags=re.S)
    assert match, "jobColumns() has been renamed or restructured — check this assertion still holds"
    assert "notified" not in match.group(1)


def test_the_exemption_is_still_earned():
    """The one exempted file must actually be the comparison it claims to be.

    An exemption that outlives its reason is how an allowlist becomes a hole. If
    badge-check.ts no longer reads the column, either it has been rewritten or
    Phase 1g has dropped the column — and in both cases the exemption above
    should go rather than sit there permitting something nothing needs.
    """
    for name in EXEMPT:
        path = DASHBOARD / name
        assert path.exists(), f"{name} is exempted but does not exist"
        code = strip_comments(path.read_text(encoding="utf-8"))
        assert any(f in code for f in FORBIDDEN), (
            f"{name} no longer reads jobs.notified, so its exemption in EXEMPT is stale. "
            f"If Phase 1g has dropped the column, delete both."
        )
