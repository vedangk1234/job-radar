"""diff_snapshots: pure set arithmetic, no database, no clock.

These are the cheapest tests in the suite and the ones that matter most —
every guard in loop.py is only as trustworthy as the split underneath it.
"""

from __future__ import annotations

from crawler.diff import diff_snapshots


def test_first_ever_fetch_is_all_new():
    d = diff_snapshots(set(), set(), {"a", "b", "c"})
    assert d.truly_new == {"a", "b", "c"}
    assert d.reappeared == frozenset()
    assert d.missing == frozenset()


def test_identical_snapshot_produces_nothing_new():
    known = {"a", "b", "c"}
    d = diff_snapshots(known, set(), known)
    assert d.truly_new == frozenset()
    assert d.reappeared == known
    assert d.missing == frozenset()


def test_added_and_removed_split_correctly():
    d = diff_snapshots({"a", "b"}, set(), {"b", "c"})
    assert d.truly_new == {"c"}
    assert d.reappeared == {"b"}
    assert d.missing == {"a"}


def test_previously_closed_job_is_reappeared_not_new():
    """The single most important line in the module.

    A job we closed and that came back was seen before, so it cannot generate
    a notification. Treating it as new is how a flapping board floods an inbox.
    """
    d = diff_snapshots(set(), {"gone"}, {"gone"})
    assert d.truly_new == frozenset()
    assert d.reappeared == {"gone"}


def test_missing_ignores_already_closed_ids():
    """A closed job that stays absent is closed, not missing again.

    Counting it would push its miss_count up on every crawl forever.
    """
    d = diff_snapshots({"open"}, {"closed"}, set())
    assert d.missing == {"open"}
    assert "closed" not in d.missing


def test_sets_are_disjoint_and_cover_fetched():
    known_open, known_closed = {"a", "b"}, {"c"}
    fetched = {"b", "c", "d"}
    d = diff_snapshots(known_open, known_closed, fetched)

    assert d.truly_new & d.reappeared == frozenset()
    assert d.truly_new | d.reappeared == fetched
    assert d.missing & fetched == frozenset()


def test_empty_fetch_marks_every_open_job_missing():
    """A board that returns nothing is reported honestly here.

    diff_snapshots has no opinion about whether that is real; refusing to act
    on it is loop.py's job (GUARD 2), and keeping that judgment out of here is
    what makes both halves testable.
    """
    d = diff_snapshots({"a", "b"}, set(), set())
    assert d.missing == {"a", "b"}
    assert d.truly_new == frozenset()


def test_inputs_are_not_mutated():
    known_open, known_closed, fetched = {"a"}, {"b"}, {"c"}
    diff_snapshots(known_open, known_closed, fetched)
    assert (known_open, known_closed, fetched) == ({"a"}, {"b"}, {"c"})
