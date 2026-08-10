"""Every distinct raw location string in the jobs table, and WHY each one is
labelled the way it is. Read-only; this script writes nothing, ever.

    python scripts/survey_all_locations.py --out survey.txt   # everything, to a file
    python scripts/survey_all_locations.py                    # everything, to stdout
    python scripts/survey_all_locations.py --min 2            # seen twice or more
    python scripts/survey_all_locations.py --open             # live rows only
    python scripts/survey_all_locations.py --section residue  # one section

WHY A SECOND SURVEY SCRIPT
--------------------------
scripts/survey_locations.py answers "what is sitting in the two buckets I drop?"
— the right question when the vocabulary is three cities and the buckets are
small enough to read. The 25-city rewrite asks a different one: what does the
WHOLE table say, and for each string, which rule decided it.

The difference is not size, it is selection bias. An alias table built from the
first script's output is built from the strings that happen to be failing today.
An alias table built from this one is built from every spelling that exists —
including the ones currently landing on the right label by luck, and the ones a
single board writes three times.

THE "MARKER FIRED" COLUMN IS THE POINT
--------------------------------------
`other` today is produced by roughly 170 different markers spread across three
tables. Without knowing which one fired, a reviewer cannot tell "Dubai ->
non-india because 'dubai' matched" from "... because 'uae' matched" from a
coincidence involving some substring nobody meant. An alias table is only
reviewable if every verdict is traceable to the token that caused it, so this
script REPLAYS fetchers.normalize's real precedence rather than paraphrasing it
— which is why it imports the private marker tables. A paraphrase is how a
survey starts lying, and a lying survey is worse than no survey: it launders a
guess into evidence.

THE SECTIONS
------------
  header    row count, distinct-string count, and the current verdict spread
  drift     how far the STORED jobs.city has already fallen behind the labeller
  distinct  every distinct location string: count, verdict, marker, companies
  residue   the strings that matched NO marker at all — the adjudication list
  tokens    every word >= 3 letters, by row count — where an unimagined
            spelling becomes visible
  fuzzy     tokens that ALMOST match a canonical city name — how `banglore` was
            originally found

RESIDUE IS THE ONE THAT BLOCKS
------------------------------
A string in `residue` is a string being labelled by a fallback rather than by a
rule. Every one of them has to be adjudicated by hand into a marker list before
the relabel is applied, because "we could not tell" and "nobody wrote a rule for
Vellore" are different facts wearing the same label. See scripts/relabel_cities.py.
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.connection import get_crawler_conn  # noqa: E402
from fetchers import normalize as N  # noqa: E402

# Grouped in SQL rather than in Python: ~9,000 rows collapse to a few hundred
# distinct (location, city, status, company) tuples, and the counts are the
# whole point of the survey.
LOAD = """
SELECT j.location, j.city, j.status, c.name AS company, count(*) AS n
  FROM jobs j
  JOIN companies c ON c.id = j.company_id
 GROUP BY j.location, j.city, j.status, c.name
"""

# The canonical names the alias table is being built toward, for the fuzzy pass
# ONLY. This is not a marker list and it labels nothing; it exists to ask "is
# this string SIMILAR to something we mean to recognise?". Both the modern and
# the old name are here on purpose — a board writing "Mysore" should surface as
# a near-miss against "mysore" and not be scored against "mysuru" alone.
CANONICAL = (
    "bengaluru", "bangalore", "hyderabad", "secunderabad", "pune", "mumbai",
    "navi mumbai", "thane", "new delhi", "delhi", "gurugram", "gurgaon",
    "noida", "chennai", "kolkata", "ahmedabad", "indore", "nagpur", "nashik",
    "aurangabad", "chhatrapati sambhajinagar", "coimbatore", "madurai",
    "hubballi", "hubli", "mangaluru", "mangalore", "mysuru", "mysore",
    "belagavi", "belgaum",
)

SECTIONS = ("drift", "distinct", "residue", "tokens", "fuzzy")

TOKEN = re.compile(r"[a-z]{3,}")

# A ratio low enough to catch a dropped vowel ("banglore" ~ "bangalore" = 0.94,
# "hydrabad" ~ "hyderabad" = 0.94) and high enough that the output stays
# readable. A hit here is a CANDIDATE, never a marker.
FUZZY_FLOOR = 0.78


def why(location: str | None) -> tuple[str, str]:
    """(label, "source:marker") — the verdict, and the rule that produced it.

    This used to flatten and replay the marker tables here, so the survey could
    report WHICH rule fired rather than only the verdict. fetchers.normalize now
    exposes exactly that as `explain_label`, and sharing it matters more than it
    looks: scripts/relabel_cities.py gates on the same question — "was this row
    decided by a rule, or by the fallback?" — and two implementations of one
    predicate is how a survey certifies a residue the relabel then refuses.

    "<no marker>" is the residue: no table matched, and the label is a fallback.
    """
    label, marker = N.explain_label(location)
    return label, marker if marker is not None else "<no marker>"


MARKER_COUNT = sum(len(m) for m in N._CITY_MARKERS.values()) + sum(
    len(table) for _source, _label, table in N._NON_CITY_TABLES
)


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--min", type=int, default=1,
                    help="hide strings/tokens seen fewer than N times (default: 1, show all)")
    ap.add_argument("--open", action="store_true", help="live rows only (status='open')")
    ap.add_argument("--out", help="write the report to this file instead of stdout")
    ap.add_argument("--section", default="all",
                    help="all | " + " | ".join(SECTIONS))
    args = ap.parse_args()

    if args.section != "all" and args.section not in SECTIONS:
        print(f"unknown section {args.section!r}; pick one of: all, {', '.join(SECTIONS)}")
        return 2

    conn = await get_crawler_conn()
    try:
        rows = await conn.fetch(LOAD)
    finally:
        await conn.close()

    if args.open:
        rows = [r for r in rows if r["status"] == "open"]

    out: list[str] = []
    p = out.append

    # distinct location string -> [row count, {companies}, Counter(stored label)]
    # NULL and the empty string are different facts and stay distinguishable.
    agg: dict[str, list] = {}
    for r in rows:
        key = "<NULL>" if r["location"] is None else r["location"]
        entry = agg.setdefault(key, [0, set(), Counter()])
        entry[0] += r["n"]
        entry[1].add(r["company"])
        entry[2][r["city"] or "NULL"] += r["n"]

    def raw(key: str) -> str | None:
        """The value to hand the labeller: the <NULL> placeholder is not a string."""
        return None if key == "<NULL>" else key

    total = sum(r["n"] for r in rows)
    p("=" * 100)
    p("SURVEY: every distinct location string in the jobs table")
    p("=" * 100)
    p(f"{total} job row(s){' (open only)' if args.open else ''}, "
      f"{len(agg)} distinct location string(s), "
      f"{MARKER_COUNT} markers in the current tables")

    verdicts: Counter[str] = Counter()
    verdict_strings: Counter[str] = Counter()
    for key, (n, _c, _s) in agg.items():
        verdict = why(raw(key))[0]
        verdicts[verdict] += n
        verdict_strings[verdict] += 1
    p("\ncurrent _classify verdict spread:")
    p(f"    {'verdict':<18} {'rows':>7}  {'strings':>8}")
    for verdict, n in verdicts.most_common():
        p(f"    {verdict:<18} {n:>7}  {verdict_strings[verdict]:>8}")
    p("\n  (city_label collapses india-elsewhere + non-india + unknown into `other`")
    p("   today; the 25-city rewrite is what stops that collapse.)")

    want = args.section

    def on(name: str) -> bool:
        return want in ("all", name)

    # --- drift ------------------------------------------------------------
    # How far the STORED labels have already fallen behind the labeller as it
    # stands right now, BEFORE any of this slice's changes. Non-zero here means
    # scripts/relabel_cities.py already had work to do, and that work has to be
    # told apart from the work this slice creates.
    if on("drift"):
        p("\n" + "=" * 100)
        p("DRIFT — stored jobs.city vs what the CURRENT labeller says")
        p("=" * 100)
        drift: Counter[tuple[str, str]] = Counter()
        for key, (_n, _c, stored) in agg.items():
            new = N.city_label(raw(key))
            for old, k in stored.items():
                if old != new:
                    drift[(old, new)] += k
        p(f"  {sum(drift.values())} row(s) already stale before this slice touches anything")
        for (old, new), n in drift.most_common():
            p(f"    {old:<16} -> {new:<16} {n:>6}")
        if not drift:
            p("    (none — every stored row carries the label the current rules give it)")

    # --- distinct ---------------------------------------------------------
    if on("distinct"):
        p("\n" + "=" * 100)
        p("DISTINCT LOCATION STRINGS")
        p("=" * 100)
        p("  count  [verdict]  marker that fired  |  raw string")
        p("  a trailing  stored=X  means the stored jobs.city disagrees with the labeller")
        p("")
        shown = 0
        for key, (n, companies, stored) in sorted(agg.items(), key=lambda kv: (-kv[1][0], kv[0])):
            if n < args.min:
                continue
            shown += 1
            verdict, marker = why(raw(key))
            new = N.city_label(raw(key))
            stale = ", ".join(f"stored={old}" for old in sorted(stored) if old != new)
            who = ", ".join(sorted(companies))
            line = f"{n:>5}  [{verdict:<15}]  {marker:<28}  {key}"
            if stale:
                line += f"   ({stale})"
            p(line)
            # One line when the string belongs to a single board, two when it is
            # shared — a per-tenant format is the thing worth seeing, and it is
            # invisible if the company list is always folded away.
            p(f"       {who}" if len(who) <= 90 else f"       {who[:90]}... ({len(companies)} companies)")
        p(f"\n  ({shown} of {len(agg)} distinct string(s) shown at --min {args.min})")

    # --- residue ----------------------------------------------------------
    if on("residue"):
        p("\n" + "=" * 100)
        p("RESIDUE — strings that matched NO MARKER IN ANY TABLE")
        p("=" * 100)
        p("  These are labelled by a FALLBACK rather than by a rule. Every one has")
        p("  to be adjudicated by hand into a marker list before the relabel is")
        p("  applied: `we could not tell` and `nobody wrote a rule for this place`")
        p("  are different facts, and only the first one deserves india-unclear.")
        p("")
        residue = [
            (key, n, companies)
            for key, (n, companies, _s) in agg.items()
            if why(raw(key))[1] == "<no marker>"
        ]
        p(f"  {len(residue)} distinct string(s), {sum(n for _k, n, _c in residue)} row(s)")
        p("")
        for key, n, companies in sorted(residue, key=lambda t: (-t[1], t[0])):
            p(f"{n:>5}  {key}")
            p(f"       {', '.join(sorted(companies))[:110]}")
        if not residue:
            p("  (empty — every string in the table is decided by a rule)")

    # --- tokens -----------------------------------------------------------
    if on("tokens") or on("fuzzy"):
        counts, seen_in = token_counts(agg)

    if on("tokens"):
        p("\n" + "=" * 100)
        p("TOKEN FREQUENCY — every word >= 3 letters, by job rows containing it")
        p("=" * 100)
        p("  This is the view an unimagined spelling shows up in. Read it looking")
        p("  for words you do not recognise, not for words you expect.")
        p("")
        for tok, n in counts.most_common():
            if n >= args.min:
                p(f"{n:>6}  {tok}")

    # --- fuzzy ------------------------------------------------------------
    if on("fuzzy"):
        p("\n" + "=" * 100)
        p(f"FUZZY NEAR-MISSES — token ~ a canonical city name, ratio >= {FUZZY_FLOOR}")
        p("=" * 100)
        p("  How `banglore` was found. Exact matches and words that are already part")
        p("  of a canonical name are excluded, so what is left is spellings we do NOT")
        p("  currently recognise. A hit here is a CANDIDATE: it earns a marker only")
        p("  because a real posting carries it, which is why the example strings and")
        p("  the row count are printed alongside.")
        p("")
        exact = {word for name in CANONICAL for word in name.split()}
        hits = 0
        for tok, n in counts.most_common():
            if tok in exact:
                continue
            best, best_ratio = None, 0.0
            for name in CANONICAL:
                ratio = difflib.SequenceMatcher(None, tok, name).ratio()
                if ratio > best_ratio:
                    best, best_ratio = name, ratio
            if best_ratio >= FUZZY_FLOOR:
                hits += 1
                examples = " | ".join(sorted(seen_in[tok])[:2])
                p(f"{n:>6}  {tok:<24} ~ {best:<26} ({best_ratio:.2f})  e.g. {examples[:70]}")
        if not hits:
            p("  (none — no token in the table is a near-miss for a canonical name)")

    text = "\n".join(out)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out}  ({len(out)} lines, {len(text)} chars)")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
