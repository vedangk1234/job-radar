"""The diffing engine: three sets, no I/O.

The governing rule of the whole tool lives here, and it is deliberately small:

    a job is NEW iff its canonical_id was not in this company's stored state.

Not "posted recently". Not "source_posted_at is within 24h". Vendor posted-at
fields are advisory and frequently useless — Workday's is the literal string
"Posted 3 Days Ago", Greenhouse re-stamps `updated_at` when a recruiter edits a
typo, and a board that re-publishes its whole feed would flood the inbox under
any date-based rule. Set membership against what we ourselves observed last
time is the only definition that cannot be gamed by a vendor's date handling.

The jobs table IS the running snapshot; there is no separate snapshot table.
Hence three inputs and three outputs, and nothing else:

    known_open   what we last saw on the board and believe is still live
    known_closed what we saw once, then stopped seeing for long enough to close
    fetched      what the board returned just now

This module is pure on purpose. Every guard in loop.py can be reasoned about
only if the set arithmetic underneath it is boring, and boring is testable
without a database, a network, or a clock.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DiffResult:
    """The three-way split of one board fetch against one stored state.

    The three sets are disjoint by construction, and
    `truly_new | reappeared == fetched_ids`.
    """

    truly_new: frozenset[str]
    reappeared: frozenset[str]
    missing: frozenset[str]

    def __repr__(self) -> str:  # sorted, so failing tests read as a diff
        return (
            f"DiffResult(truly_new={sorted(self.truly_new)}, "
            f"reappeared={sorted(self.reappeared)}, "
            f"missing={sorted(self.missing)})"
        )


def diff_snapshots(
    known_open_ids: set[str],
    known_closed_ids: set[str],
    fetched_ids: set[str],
) -> DiffResult:
    """Split `fetched_ids` against what we already knew about this company.

    truly_new
        Never seen before, in any state. This is the only set that can ever
        produce a notification, and only on a non-seeding run.

    reappeared
        Seen before — either still open (the overwhelmingly common case: a job
        that was there yesterday and is there today) or previously closed and
        now back. Both collapse into one set because both mean the same thing
        for alerting: we already told the user about this, or deliberately
        chose not to. A flap must never read as news.

    missing
        Open last time, absent now. Note this is computed against
        `known_open_ids` only: an already-closed job that stays absent is not
        "missing again", it is simply closed, and re-counting it would push
        miss_count up forever.

    Absence is a single observation, not a verdict. This function reports it;
    loop.py requires three consecutive ones before anything is closed.
    """
    known_open = frozenset(known_open_ids)
    known_closed = frozenset(known_closed_ids)
    fetched = frozenset(fetched_ids)
    known_any = known_open | known_closed

    return DiffResult(
        truly_new=fetched - known_any,
        reappeared=fetched & known_any,
        missing=known_open - fetched,
    )
