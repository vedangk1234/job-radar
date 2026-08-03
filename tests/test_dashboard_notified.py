"""The dashboard does not read jobs.notified — the mirror of the crawl-path invariant.

Phase 1d moved "this person has been emailed about this job" into
user_job_notifications and 1e moved the digest queue onto it, which left exactly
one reader of the old global boolean: the dashboard's NEW / NOTIFIED badge. Phase
1f moved that too, and Phase 1g dropped the column.

This file is what made the drop a no-op that could not surprise anybody, and it
is why it stays after the drop rather than going with it. The column is gone from
the database, so a re-introduced read would now fail loudly rather than quietly —
but it would fail in whatever ran it, which for this app means a page render in
production or a check nobody runs weekly. Catching the NAME on the commit that
adds it is still the cheaper failure, and the rule it enforces outlives the
column: per-user state does not go back onto a shared row.

It is the same shape as the assertions in tests/test_user_companies.py and
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

AND THE DOT IS ANCHORED, WHICH IS NOT PEDANTRY — IT IS THIS PROJECT'S OWN RULE.
`.notified` as a plain substring is wrong in the direction that matters, and the
counter-example is in the tree: `lib/jobs.notifiedToUser` is a namespace import
reaching the per-user PREDICATE, the very thing that replaced the column, and it
contains `.notified` character for character. So does `.notified_at`. A substring
ban would forbid the replacement while forbidding the original, which is the
failure mode notify/match.py spends four paragraphs on — `go` inside "Django",
`qa` inside "Qatar" — arriving in a test instead of in a filter. Every pattern
here is therefore word-boundary anchored on the right, exactly as every term in the
matcher is. `j.notified` matches; `.notifiedToUser` does not.

That anchoring was added in Phase 1g, when badge-check.ts lost its exemption and
became the first file the rule had ever been asked about that legitimately uses
the replacement by name. The exemption had been hiding the imprecision.

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
# rather than collapsed to the last alone so the failure message names the thing
# that was actually written.
#
# Right-anchored on a word boundary so that `notifiedToUser` and `notified_at` — the
# replacement and the timestamp — are not caught by a rule aimed at the column.
# See the module docstring on why a bare substring is the wrong tool here and
# where this project already knew that.
FORBIDDEN = (r"j\.notified\b", r"jobs\.notified\b", r"\.notified\b")


def reads_the_column(code: str) -> str | None:
    """The first forbidden form present, as it would be written. None if clean."""
    for pattern in FORBIDDEN:
        match = re.search(pattern, code)
        if match:
            return match.group(0)
    return None

# NO EXEMPTIONS. There was exactly one, and it expired in Phase 1g.
#
# scripts/badge-check.ts held it, because §1 of that script was the migration's
# own before/after comparison — it read both the retiring boolean and the per-user
# table for the same user and failed if they disagreed about one job, which it
# could only do by naming both sides. It was enumerated here rather than left as a
# hole, and a second test asserted that the file still read the column, so an
# exemption that stopped being earned would fail rather than quietly widen what
# was allowed.
#
# 1g dropped the column, that section stopped being possible, and the same diff
# deleted the section, the exemption and the test guarding it. The list is gone
# rather than empty: an empty allowlist is an invitation, and there is now no
# argument that could put a file back on it — the column does not exist.


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


@pytest.mark.parametrize("path", dashboard_sources(), ids=relative)
def test_no_dashboard_file_reads_the_notified_column(path: Path):
    """Every file, with nothing excepted.

    This was the precondition Phase 1g depended on: if it passed, dropping
    jobs.notified could not change what any screen showed, because no screen
    consulted it. It now records the outcome instead — the column is gone, and a
    file naming it again would be naming nothing. That is the same argument
    tests/test_user_companies.py makes about the crawl never having heard of the
    watch list, applied to a column instead of a table.
    """
    code = strip_comments(path.read_text(encoding="utf-8"))
    found = reads_the_column(code)
    assert found is None, (
        f"{relative(path)} reads the dropped notification column ({found!r}). "
        f"Per-user notification state is a row in user_job_notifications — see "
        f"the notifiedToUser fragment in lib/jobs.ts."
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
