"""Everything the digest touches in the database, and nothing that decides anything.

Eight operations, and the split between them is the whole safety story:

    load_preferences           read ONE person's filter
    load_active_preferences    read EVERY active person's filter — the union the
                                                crawl's enrichment pass works from
    fetch_candidates           read the queue  (open, not back catalogue, not yet
                                                sent to THEM)
    fetch_suppression_candidates  the same two clauses over ANY status — what
                                                could still be owed to somebody
    suppress_filter_backlog    write the history a new or widened filter admits
    fetch_enrichment_targets   read the jobs whose description is unread
    save_description_facts     write what reading a description taught us
    record_sent                write the receipt, ONLY after Resend returned an id

save_description_facts is the only write in this file that is not a receipt, and
it writes four scalars and a clock reading: min_experience, is_pm_role,
desc_checked, desc_fetch_failed, desc_checked_at. It has no parameter for the
description itself, which is the point — see notify/description.py. There is no
column for one and no statement here that could fill it.

fetch_enrichment_targets is read by the CRAWLER (crawler/enrich.py) rather than
by the digest, which is why it does not filter on notification state at all: the
dashboard shows every open job that fits, emailed or not, so every open job that
fits has to have had its description read. That difference is the whole bug fix;
the digest's own queue is still fetch_candidates.

THE QUEUE IS ONE PERSON'S, SINCE PHASE 1e — AND FILTER-FIRST SINCE PHASE 3
---------------------------------------------------------------------------
fetch_candidates answers "what is open, not back catalogue, and not already sent
to THEM" — two clauses, two different questions, spelled out above
CANDIDATES_SQL. Every read in this module that decides anything takes a user_id,
and the fan-out in notify/digest.py calls them once per user.

It asked a third question until the filter-first slice: "is this job's company
one THIS user watches". Company tracking is unlimited and free now, so every user
effectively sees the whole catalog and the thing that decides what reaches an
inbox is a person's FILTER rather than their watch list. `user_companies` still
exists and /watchlist still writes it; it is an overlay, and nothing in this
module reads it.

Removing that clause moved an obligation rather than deleting one. The gate was
also what stopped a first digest being an archive dump, paired with the
suppression db/watchlist.track_companies used to run when a pair was created —
so suppress_filter_backlog is where that now lives, triggered by the filter
changing instead of by a company being tracked.

The global jobs.notified boolean is gone, as of Phase 1g. It was doing two jobs
at once: "this is back catalogue" (a fact about a crawl) and "somebody was
emailed this" (a fact about a person). 1d moved the person half into
user_job_notifications; 1e moved the crawl half into jobs.seeded, which
crawler/store.mark_new writes from the same seeding flag — so the crawl path
stays user-blind and the queue stops flattening two users into one. 1d through 1f
kept writing the boolean as a rollback net; 1g removed the last write and then
the column, once the fan-out had been proven in production.

record_sent is deliberately the last thing that happens in a digest run, after
the network call has already succeeded. That ordering is the same idea as the
crawler's Guard 1: when the outcome is in doubt, change nothing. A send that
fails leaves the queue exactly as it was, so the next run simply tries again —
whereas recording the send first and then failing would lose the job silently and
forever, which is the one failure this whole table exists to prevent.

The two writes inside record_sent run in one transaction, so it is impossible to
end up with a job recorded as notified that has no row in the sent-log.

SQL portability note (same as crawler/store.py): these statements stay inside
the subset Postgres and SQLite agree on — numbered placeholders, `now()`, plain
joins — so the tests run the production queries against an in-memory database
rather than a mock of them.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Mapping

from db.notifications import record_notified

from .match import Preferences, is_match

# No CHUNK constant here any more. This module held one until Phase 1g, for the
# IN-list in record_sent's boolean flip; the two statements that remain bind
# either one parameter or a list of records through executemany, and the chunking
# that a digest's worth of ids genuinely needs lives with the statement that
# needs it — db/notifications.record_notified.
# SEVEN COLUMNS SINCE PHASE 3 SLICE 2, and the one it still does not name.
#
# The three experience columns arrived empty in Step 0 and are read here for the
# first time: exp_range_low / exp_range_high are the looking range, and
# show_no_experience_jobs decides what a NULL jobs.min_experience means for this
# account. notify/match.experience_excluded is what does something with them.
#
# years_of_experience_held IS DELIBERATELY NOT SELECTED. It is how much experience
# the person HAS rather than what they are looking for, it is informational, and
# it must never filter anything — so the statement that feeds the matcher does not
# fetch it, notify.match.Preferences has no field for it, and
# tests/test_phase3_columns.py asserts textually that no matcher in either
# language names it. Three independent reasons it cannot reach a verdict, none of
# which is somebody remembering.
#
# NINE COLUMNS SINCE CITIES SLICE B, and `cities` is still one of them ON PURPOSE.
# selected_cities and all_india are the new gate; `cities` is the legacy column
# the fallback in notify.match.Preferences.effective_cities reads when an account
# predates scripts/backfill_onboarding_prefs.py. Selecting all three is what makes
# that fallback reachable — and it is the one place in this file where a column is
# selected for a reader that will be DELETED rather than added to, which is why
# the sentence naming the contract slice lives on the property rather than here.
#
# TEN COLUMNS SINCE THE WORK-MODE SLICE. work_modes is the multi-select of
# arrangements this account will accept, read by notify.match.work_mode_ok. It is
# the fourth member of the family that matches NOTHING when blank — with the
# unconfigured experience range and the blank city answer — and PUT
# /api/preferences refuses to save an empty one for exactly that reason.
#
# ELEVEN SINCE THE SALARY SLICE. salary_bands is the multi-select of pay bands
# this account will accept, read by notify.match.salary_ok. It is the one member
# of this list that is NOT fail-closed when blank: an empty selection means "do
# not filter on pay" rather than "match nothing", which is the opposite of what
# work_modes, selected_cities and the experience range do. See salary_ok for the
# argument — briefly, the job side of this rule is silent on ~100% of the board,
# so blank is the filter's resting state rather than an unanswered question, and
# fail-closed here could not produce the empty-digest failure the other three are
# arranged against because it would empty nothing.
PREFERENCES_SQL = (
    "SELECT cities, selected_cities, all_india, keywords, exclusions, max_level, "
    "exp_range_low, exp_range_high, show_no_experience_jobs, work_modes, salary_bands "
    "FROM preferences WHERE user_id = $1"
)

# Asked only when the statement above found nothing, and only to tell two very
# different silences apart. See load_preferences.
UNOWNED_PREFERENCES_SQL = "SELECT count(*) FROM preferences WHERE user_id IS NULL"

# EVERY ACTIVE ACCOUNT'S FILTER, AND WHY THAT IS A DIFFERENT QUESTION FROM THE
# ONE ABOVE. Phase 2b: crawler/enrich.py used to resolve "the current user" from
# MY_EMAIL and enrich against that one person's filter, which was the last place
# on the crawl path that had an opinion about who is running the script. With a
# second account whose keywords differ, that user's matches reached their digest
# with desc_checked = FALSE — min_experience NULL, and unknown means KEEP, which
# is the Step 7c leak arriving by a new route.
#
# So enrichment reads the UNION of these instead: a description is worth
# fetching if it is worth fetching for ANYBODY. That is the same shape as "crawl
# once, fan out" — the reading is user-blind and shared (one description read
# serves every user who can see the job), and only the sending narrows.
#
# JOINED TO public.users RATHER THAN READ ALONE, so the set of filters
# enrichment works from and the set of people the fan-out sends to are the same
# set by construction: is_active is the account-level off switch, and
# db/users.DIGEST_USERS_SQL reads it for exactly the same reason. A deactivated
# account is not owed a description fetch.
#
# Schema-qualified public.users, never a bare `users`: Supabase ships its own
# auth.users, and db/connection.py sets no search_path. See db/schema.sql.
#
# ORDER BY u.id so a pass is deterministic. Nothing depends on the order — the
# candidate test is an OR over all of them — but a run whose output moves between
# invocations for no reason is harder to compare against yesterday's.
#
# THE SAME NINE COLUMNS AS PREFERENCES_SQL, which is not a coincidence to be
# tidied later: both build a notify.match.Preferences, so a column one of them
# selects and the other does not is a filter that means two things depending on
# which entry point loaded it. The enrichment union would then narrow on a rule
# the digest does not apply, or the reverse.
#
# CITIES SLICE B IS THE FIRST TIME THAT WARNING HAD TEETH. Had only PREFERENCES_SQL
# gained selected_cities, every account's enrichment filter would have fallen back
# to the legacy `cities` column while its digest read the new one — so an account
# that moved from blr to mumbai would have its Mumbai matches enter the digest with
# desc_checked = FALSE, which means min_experience NULL, which means KEEP. That is
# the Step 7c leak arriving by a third route, and the only thing preventing it is
# that these two statements are edited together.
#
# THE WORK-MODE SLICE IS THE SECOND TIME THAT WARNING HAD TEETH, and it bites
# harder than the cities one did. work_modes is read by title_stage_ok, which is
# what narrows this very pass — so had only PREFERENCES_SQL gained the column,
# enrichment would union over filters whose work_modes was the empty fallback,
# every candidate test would answer False, and the pass would read NOTHING at
# all. A silent, total stop rather than a leak; the same edit-them-together rule
# catches both.
#
# THE SALARY SLICE IS THE THIRD TIME THAT WARNING HAD TEETH, and it bites in the
# gentlest of the three ways — which is worth saying, because "this one is
# harmless" is how the pair drifts apart. salary_bands is read by is_match but
# NOT by title_stage_ok, so an enrichment union missing it would not stop the pass
# (the work-mode failure) and would not narrow it wrongly (the cities failure); it
# would simply give every filter DEFAULT_SALARY_BANDS, which filters nothing, on a
# pass whose candidate test does not consult pay anyway. The two statements are
# still edited together, because the next column may not be so forgiving.
ACTIVE_PREFERENCES_SQL = """
SELECT p.cities, p.selected_cities, p.all_india,
       p.keywords, p.exclusions, p.max_level,
       p.exp_range_low, p.exp_range_high, p.show_no_experience_jobs,
       p.work_modes, p.salary_bands
  FROM preferences p
  JOIN public.users u ON u.id = p.user_id
 WHERE u.is_active = TRUE
 ORDER BY u.id
"""

# The queue, joined to its companies so the digest can group by employer without
# a second round trip. Ordered by company then title so the rendered email is
# stable: the same match set always produces the same HTML.
#
# c.ats / c.token / c.config come along because the second-stage filter has to
# ask the right vendor API for this job's description, and that is the company's
# knowledge, not the job's. The three description-derived columns come along so
# is_match can apply them without a second query — and so a job already checked
# is visibly already checked, which is what stops it being fetched twice.
_CANDIDATES_TEMPLATE = """
SELECT j.canonical_id, j.title, j.location, j.city, j.url,
       j.first_seen_at, j.company_id,
       j.min_experience, j.is_pm_role, j.desc_checked,
       j.desc_fetch_failed, j.desc_checked_at,
       c.name AS company, c.ats, c.token, c.config
  FROM jobs j
  JOIN companies c ON c.id = j.company_id
 WHERE {status_filter}{queue_filter}
 ORDER BY c.name, j.title
"""

# THE STATUS CLAUSE IS A PARAMETER BECAUSE ONE READER NEEDS IT OFF, and exactly
# one: the filter-backlog suppression below. Everything that decides an EMAIL is
# open-only — a closed posting is not something to apply to, and enriching or
# sending one would be spending attention on a job nobody can take.
#
# The suppression is the opposite case and the reason is crawler/store.UPSERT_SEEN:
# a closed job that reappears comes back `status = 'open'` with `miss_count = 0`
# while KEEPING its canonical_id. So a suppression scoped to open rows leaves a
# hole exactly one flap wide — the job sits closed and unsuppressed while somebody
# saves their filter, the vendor re-lists it a week later, and it arrives in that
# person's digest as news because a board was edited. `reappeared` is precisely the
# set crawler/diff.py refuses to treat as news, and this is that refusal carried
# into the notify half.
#
# Spelled as two named constants rather than as an `if`, so both spellings are
# visible in one place and neither reader can drift onto the other's.
_OPEN_ONLY = "j.status = 'open'"
_ANY_STATUS = "TRUE"

# THE QUEUE FILTER — TWO CLAUSES SINCE THE FILTER-FIRST SLICE, and it is shared
# verbatim between the queue and the suppression so the two can never disagree
# about what "already handled" means.
#
#   j.seeded = FALSE         "is this job back catalogue?"
#                            WHAT KIND — a property of the crawl, user-blind and
#                            permanent. This clause is the silent seed.
#   NOT EXISTS user_job_...  "have YOU been told about it?"
#                            WHO ALREADY KNOWS — per person, since 1d.
#
# WHY THE SUPPRESSION READ CARRIES `j.seeded = FALSE` TOO, which is not obvious.
# A seeded row is excluded from every queue forever by this very clause, so
# recording one as already-notified buys nothing and costs a row per user per
# seeded job — 6,496 of them from a single afternoon's discovery sweep, times
# everyone who ever signs up. That is the arithmetic db/notifications.py refuses
# in its own words ("the silent seed is deliberately not handled here"), and it is
# refused here for the same reason: the crawl's fact stays on the crawl's column.
_QUEUE_FILTER = """
   AND j.seeded = FALSE
   AND NOT EXISTS (
         SELECT 1
           FROM user_job_notifications n
          WHERE n.user_id = $1
            AND n.canonical_id = j.canonical_id
       )"""

# THE QUEUE — ONE USER'S, AND TWO CLAUSES THAT ANSWER TWO DIFFERENT QUESTIONS.
#
# IT WAS THREE UNTIL THE FILTER-FIRST SLICE, and the one that went is the reason
# this comment is long. The queue opened with
#
#     EXISTS (user_companies uc WHERE uc.user_id = $1
#                                 AND uc.company_id = j.company_id)
#
# — "is this job's company one you asked about?" — and that clause was the GATE:
# a person matched jobs at companies they watched, and nowhere else. It was the
# right shape while a watch list was how somebody said what they wanted to hear
# about, and it stopped being the right shape the moment company tracking became
# unlimited and free. Every user effectively sees the whole catalog now, so the
# thing that decides what reaches an inbox is a person's FILTER, and a gate in
# front of it could only ever subtract from an answer the filter had already
# given. An account watching nothing used to match nothing however good its
# keywords were; it matches the catalog now, which is what a filter is for.
#
# The watch list is NOT gone and this is not a deprecation. `user_companies`
# still exists, /watchlist still writes it, the per-plan cap still holds — it is
# simply an overlay rather than a gate, and nothing reads it to decide an email.
# See db/watchlist.py, which keeps the note about what would have to come back
# if it ever became a filter dimension again.
#
# WHAT REPLACED THE GATE'S OTHER JOB. The clause was doing something besides
# narrowing: paired with the suppression in db/watchlist.track_companies, it was
# what stopped a person's FIRST digest being an archive dump. Creating a tracking
# row recorded that company's stored jobs as already-notified, so history could
# not walk in behind the subscription. With no gate there is no tracking moment to
# hang that on, so the obligation moved to where the match set is now decided —
# see suppress_filter_backlog below. The hazard did not go away; its trigger did.
#
# WHY THE BOOLEAN CLAUSE IS GONE, AND WHAT REPLACED IT. `j.notified = FALSE` was
# never one rule. It meant "not seeded AND not emailed", and the second half of
# that is a statement about a PERSON — which is why a job emailed to me silenced
# it for everybody, the flattening tests/test_user_notifications.py spelled out
# in 1d and the exact failure user_job_notifications exists to prevent. So the
# two halves were separated rather than swapped: the person half moved into the
# table (1d), and the crawl half moved into jobs.seeded, which crawler/store.
# mark_new writes from the same `seeding` flag it always used. The crawl path
# still knows nothing about users, and still may not.
#
# $1 appears twice, where it used to appear three times. asyncpg repeats a bound
# parameter natively and the test shim's _translate maps placeholders in order of
# appearance precisely so that works, so the statement the tests run is this one
# character for character.
#
# WHAT THIS DELIBERATELY DOES NOT ASK: companies.tracked. That is the crawl's
# exclusion axis — a property of the catalog, one shared value for the whole
# install — and it was never a per-person question. It was not a candidate for
# this clause's job before the filter-first slice and it is not one now.
CANDIDATES_SQL = _CANDIDATES_TEMPLATE.format(
    status_filter=_OPEN_ONLY, queue_filter=_QUEUE_FILTER
)

# The same query minus the queue filter. Used only by scripts/preview_digest.py
# --ignore-notified, which never writes: it answers "what WOULD my filter have
# picked out of everything currently open", against a table whose rows are almost
# all already notified.
#
# It was ALREADY filter-first before the filter-first slice, which is the single
# most useful fact about this statement: it never carried the watch-list gate, so
# its total and `npm run match-parity`'s have always been comparable, and they are
# the fixed points the slice was measured against. A number that did not move is
# only evidence if it could have.
#
# Note that this one binds NO parameter, which is why fetch_candidates branches
# on how it calls conn.fetch rather than passing user_id either way: asyncpg
# rejects an argument the statement has no placeholder for. Widening the read to
# "every open job" genuinely has no user in it — that is what makes it a preview
# of the matcher rather than a preview of someone's digest.
CANDIDATES_IGNORING_NOTIFIED_SQL = _CANDIDATES_TEMPLATE.format(
    status_filter=_OPEN_ONLY, queue_filter=""
)

# THE SUPPRESSION READ — the queue's own two clauses, over jobs of ANY status.
#
# One statement away from CANDIDATES_SQL and the difference is the whole point.
# It shares _QUEUE_FILTER verbatim, so "already back catalogue" and "already told"
# mean exactly what they mean to the queue; it drops the open-only clause, so a
# closed job that matches is recorded as history now rather than arriving as news
# the week a vendor re-lists it. See _ANY_STATUS above for that argument in full.
#
# Read by suppress_filter_backlog and by scripts/suppress_filter_backlog.py, and
# by nothing that sends. It is a read of what WOULD be owed to somebody, which is
# why it is shaped like the queue rather than like the preview.
SUPPRESSION_CANDIDATES_SQL = _CANDIDATES_TEMPLATE.format(
    status_filter=_ANY_STATUS, queue_filter=_QUEUE_FILTER
)

# Every open job whose description we have not successfully read — the crawl's
# enrichment queue, before the title filter narrows it in Python.
#
# The `$1` cutoff is passed in rather than written as `now() - interval '24
# hours'` for the reason the module docstring gives about SQL portability: an
# interval literal is Postgres-only, and this statement has to run verbatim
# against the SQLite shim in tests. It is compared only against rows that
# already failed, so a clean read is never re-asked whatever the cutoff says.
#
# The status = 'open' clause is not an optimisation. A closed job is not worth a
# request, and enriching one would be the crawler spending politeness on a
# posting nobody can apply to.
ENRICHMENT_TARGETS_SQL = """
SELECT j.canonical_id, j.title, j.location, j.city, j.url,
       j.first_seen_at, j.company_id,
       j.min_experience, j.is_pm_role, j.desc_checked,
       j.desc_fetch_failed, j.desc_checked_at,
       c.name AS company, c.ats, c.token, c.config
  FROM jobs j
  JOIN companies c ON c.id = j.company_id
 WHERE j.status = 'open'
   AND (j.desc_checked = FALSE
        OR (j.desc_fetch_failed = TRUE
            AND (j.desc_checked_at IS NULL OR j.desc_checked_at < $1)))
 ORDER BY c.name, j.title
"""

INSERT_NOTIFICATION_SQL = """
INSERT INTO notifications (canonical_id, sent_at, digest)
VALUES ($1, now(), $2)
"""

# Two facts and three pieces of bookkeeping. Note what is NOT here: any column
# that could hold the description those facts were derived from — and, just as
# deliberately, any column about the job's LIFECYCLE. There is no seeded here,
# no status, no miss_count, no first_seen_at. That is what makes it safe to run
# this in the middle of a crawl: enrichment cannot re-open a closed job, cannot
# hand a back catalogue to a digest, and cannot make a job look newly seen.
#
# desc_checked = TRUE is set unconditionally, including when min_experience came
# back NULL because the fetch failed. "We looked and learned nothing" is a fact
# worth recording — the alternative is re-fetching the same dead endpoint on
# every run for as long as the posting stays open. desc_fetch_failed is what
# keeps that from meaning "and never look again": see ENRICHMENT_TARGETS_SQL.
#
# work_mode JOINED THE LIST IN THE WORK-MODE SLICE, and it is the one column here
# that is deliberately allowed to be written as NULL. The other four always carry
# a value once a row has been looked at; this one is NULL whenever the read failed
# or the vendor and the prose both said nothing readable. That NULL is a FACT —
# "nobody has established this posting's arrangement" — and notify.match.
# work_mode_of reads it as onsite for filtering WITHOUT writing onsite down. One
# column, one fact: the jobs.notified lesson, applied before the mistake instead
# of after it.
# THE SALARY TRIO JOINED IN THE SALARY SLICE, and salary_checked is the reason
# the pair can be written at all without repeating jobs.notified. salary_min NULL
# on a row this statement has touched means "read it, this posting states no pay",
# which is 100% of what the parser finds today; salary_min NULL on a row it has
# not touched means "nobody has looked". Those are different facts and they now
# live in different columns, so notify.match.salary_ok can treat both as unpriced
# — which it does, deliberately — without the TABLE losing the distinction.
#
# salary_checked is written as $8 rather than as a literal TRUE, unlike
# desc_checked directly above it. desc_checked means "we asked", which is true
# even when the fetch came back with nothing. salary_checked means "a parser saw
# a description", which a failed fetch is precisely not — so the two differ on
# exactly the desc_fetch_failed rows and cannot share a spelling.
SAVE_DESCRIPTION_FACTS_SQL = """
UPDATE jobs
   SET min_experience    = $2,
       is_pm_role        = $3,
       desc_checked      = TRUE,
       desc_fetch_failed = $4,
       work_mode         = $5,
       salary_min        = $6,
       salary_max        = $7,
       salary_checked    = $8,
       desc_checked_at   = now()
 WHERE canonical_id = $1
"""


async def load_preferences(conn, user_id) -> Preferences:
    """Read this user's preferences row.

    Keyed on the owner rather than on the old fixed address (`WHERE id = 1`).
    `user_id` is the caller's, and since Phase 2b there is no ambient answer to
    fall back on: the fan-out threads an id per active account, and the dashboard
    threads the one on the session. Nothing here reads the environment.

    A missing row falls back to the seeded defaults rather than raising, so a
    database that has not had the new schema applied yet still produces a
    correct digest instead of a stack trace. It is a fallback, not a default:
    the row is what the dashboard edits, and the row wins whenever it exists.

    THE ONE SILENCE THAT IS NOT A FALLBACK. That fallback is only honest when
    "no row for this user" means what it says. There is a second way to get zero
    rows: db/schema.sql has been applied, adding a nullable user_id, but
    scripts/backfill_preferences_user.py has not run yet — so the real row is
    sitting there unowned and this query cannot see it. Falling back then would
    replace a filter someone tuned with the seeded defaults and email the
    results, which is exactly the "quietly wrong" failure this codebase spends
    its guards on. So the two are told apart with one extra query, asked only in
    the miss path, and the half-applied case raises instead.
    """
    rows = await conn.fetch(PREFERENCES_SQL, user_id)
    if rows:
        return Preferences.from_row(rows[0])

    if await conn.fetchval(UNOWNED_PREFERENCES_SQL):
        raise RuntimeError(
            "preferences.user_id is unset on an existing row: db/schema.sql has "
            "been applied but scripts/backfill_preferences_user.py has not run. "
            "Run it before crawling or sending a digest — until then this user's "
            "filter is unreadable, and falling back to defaults would silently "
            "replace it."
        )

    return Preferences.from_row(None)


async def load_active_preferences(conn) -> list[Preferences]:
    """Every active account's filter, deduplicated, for the enrichment union.

    THE CRAWL'S ANSWER TO "WHOSE FILTER", AND IT IS DELIBERATELY NOT ONE PERSON'S.
    Reading a description is user-blind work: the two facts it produces
    (min_experience, is_pm_role) are properties of the posting, they are stored on
    the job rather than against a person, and one read serves everybody who can
    see the row. So the question the crawl has to answer is not "whose filter is
    this" but "is this job worth a request to ANYONE", and that is an OR over
    these rather than a lookup of a current user. See crawler/enrich.py.

    DEDUPLICATED because Preferences is a frozen dataclass of tuples and therefore
    hashable: an install where five accounts have never edited the seeded defaults
    is one filter, not five, and the candidate test should cost accordingly.
    dict.fromkeys rather than a set, so the order the statement chose survives.

    THE SAME TWO SILENCES load_preferences TELLS APART, told apart the same way.
    No rows can mean "no active account has a filter", which is a real answer on a
    fresh install and resolves to the seeded defaults — enrichment then reads the
    descriptions a default filter would want, which costs a few requests and can
    mislead nobody, because this pass sends no email and writes no lifecycle
    column. It can also mean db/schema.sql has been applied and
    scripts/backfill_preferences_user.py has not, in which case a real tuned
    filter is sitting there unowned and invisible to the JOIN. Falling back then
    would quietly enrich against a filter nobody chose, so that one raises.
    """
    rows = await conn.fetch(ACTIVE_PREFERENCES_SQL)
    if rows:
        return list(dict.fromkeys(Preferences.from_row(row) for row in rows))

    if await conn.fetchval(UNOWNED_PREFERENCES_SQL):
        raise RuntimeError(
            "preferences.user_id is unset on an existing row: db/schema.sql has "
            "been applied but scripts/backfill_preferences_user.py has not run. "
            "Run it before crawling — until then no filter is readable by owner, "
            "and enriching against the defaults would silently ignore the one "
            "somebody tuned."
        )

    return [Preferences.from_row(None)]


async def fetch_candidates(
    conn, user_id: Any, *, ignore_notified: bool = False
) -> list[Mapping[str, Any]]:
    """Rows eligible to be considered — before any filtering.

    Eligible means open, not stored by a silent seed, and not yet notified TO
    THIS USER. Whether a job is *interesting* is decided afterwards, by
    notify/match.py, over the rows this returns — and since the filter-first
    slice that is the ONLY thing which narrows a person's digest to a person.

    Note which of those three is not about the user: `seeded` is a property of the
    crawl that stored the row, so it excludes the same rows for everybody. That
    is what "the silent seed is silent for all of us" means, and it is the clause
    that replaced the old global notified = FALSE.

    It used to mean "in a company THIS USER tracks" as well. It does not, and an
    account watching nothing now reads the whole catalog here rather than an empty
    list — see CANDIDATES_SQL for why that is the point rather than a regression.

    `user_id` is required rather than defaulted, and that is deliberate: a
    default would be a guess about whose queue this is, and the failure mode of
    guessing wrong is silence — a digest computed for the wrong person looks
    exactly like a quiet week. Since 2b run_digest makes the same demand of its
    own caller, so the id comes from public.users by way of the fan-out and there
    is no ambient answer left anywhere on this path.

    `ignore_notified` is for preview only. Nothing that can write ever sets it,
    and in that mode `user_id` is genuinely unused because the widened question
    has no user in it.
    """
    if ignore_notified:
        rows = await conn.fetch(CANDIDATES_IGNORING_NOTIFIED_SQL)
    else:
        rows = await conn.fetch(CANDIDATES_SQL, user_id)
    return [dict(row) for row in rows]


async def fetch_suppression_candidates(conn, user_id: Any) -> list[Mapping[str, Any]]:
    """Every job that could still be owed to this user — open or closed.

    The queue's own two clauses over jobs of any status. Read only by the
    suppression below and by scripts/suppress_filter_backlog.py; nothing that
    sends an email calls it. See SUPPRESSION_CANDIDATES_SQL.
    """
    rows = await conn.fetch(SUPPRESSION_CANDIDATES_SQL, user_id)
    return [dict(row) for row in rows]


async def suppress_filter_backlog(
    conn, user_id: Any, before: Preferences | None, after: Preferences
) -> list[str]:
    """Record what a NEW OR WIDENED filter has just admitted as already-notified.

    THE OBLIGATION THAT USED TO BELONG TO TRACKING A COMPANY, MOVED TO WHERE THE
    MATCH SET IS NOW DECIDED. Until the filter-first slice the queue opened with
    `EXISTS (user_companies …)`, so the moment a person started watching a company
    every stored job of that company was in their queue — and
    db/watchlist.track_companies wrote the pair and suppressed that company's
    history in one transaction, which is what stopped a first digest being an
    archive dump. There is no gate now and therefore no tracking moment, and a
    brand-new account matches the WHOLE CATALOG the instant it has a filter. Same
    hazard, bigger blast radius, different trigger.

    So the trigger is the filter itself:

        OLD  = what the row as it stands matches
        NEW  = what the row being saved matches
        suppress (NEW \\ OLD)

    DAY ONE FALLS OUT OF THAT RULE RATHER THAN BEING A CASE IN IT, which is the
    reason it is written as a delta. A brand-new account's row is blank on every
    field, and every blank in this project is fail-closed — no selected_cities and
    no all_india matches nothing, an empty work_modes matches nothing, an
    unconfigured experience range matches nothing. So OLD is empty, NEW \\ OLD is
    NEW, and the first save records the entire current match set. Pass
    `before=None` to say so explicitly (a genuinely absent row, as opposed to a
    row that matches nothing, which is the same answer arrived at honestly).

    WIDENING MID-LIFE IS THE SAME HAZARD AND GETS THE SAME ANSWER. Ticking a
    twenty-fourth city, or turning on all-India, admits hundreds of stored rows
    that have no user_job_notifications row — because rows are written when
    something is SENT, and nothing was ever sent for a job that did not match. So
    nothing suppresses them and they go out in the next digest as news, because a
    person edited a form. That is exactly the "entering" direction
    scripts/relabel_cities.report_blast_radius refuses to apply on, one table over
    and reached from the other side: there a LABELLER moved a job into a filter,
    here the FILTER moved onto a job.

    NARROWING SUPPRESSES NOTHING, and neither does an unrelated edit. The delta is
    empty, so a job already legitimately in somebody's queue stays there — which is
    why this is a delta and not "suppress everything currently matching on every
    save". The latter would silently eat whatever arrived since the last digest
    every single time anybody touched the form.

    THE REAL MATCHER DECIDES, NOT A PREFILTER. is_match is run over both filters
    rather than pushing an approximation into SQL, and the difference is not
    academic: the SQL prefilter in dashboard/src/lib/jobs.ts is allowed to be WIDER
    than the matcher, so using its shape here would suppress rows that never
    matched — and those are precisely the rows enrichment can later turn into
    genuine matches, by filling a NULL min_experience the filter then admits. Over-
    suppressing today is swallowing a real match tomorrow.

    Returns the canonical_ids genuinely recorded. NOT wrapped in a transaction
    here: the caller holds one spanning this and the preferences UPDATE itself,
    for the reason db/watchlist.track_companies held one — between the two there
    is a state in which the filter is widened and the suppression is not written,
    and the hourly digest runs in exactly that state if the process dies there.
    """
    rows = await fetch_suppression_candidates(conn, user_id)

    admitted = {
        row["canonical_id"] for row in rows if is_match(row, after)
    }
    if before is not None:
        admitted -= {row["canonical_id"] for row in rows if is_match(row, before)}
    if not admitted:
        return []

    # Ordered so a run is reproducible and two invocations over the same state
    # produce the same report. Nothing depends on the order — record_notified is a
    # set operation — but a migration whose output moves between invocations for
    # no reason is harder to compare against the dry run somebody approved.
    ids = sorted(admitted)
    await record_notified(conn, user_id, ids)
    return ids


async def fetch_enrichment_targets(
    conn, retry_before: datetime
) -> list[Mapping[str, Any]]:
    """Open jobs whose description has not been read successfully — and only those.

    `retry_before` is the cutoff for asking a FAILED read again; a row that was
    read cleanly is never returned whatever it is set to. The caller then
    narrows this to the title-stage matches, which is where the cost argument
    lives: this returns the unchecked half of the board, and only a few dozen of
    those are worth a request.

    Deliberately not filtered on notification state of any kind. See the module
    docstring: this is the CRAWL's queue, not a digest's.
    """
    rows = await conn.fetch(ENRICHMENT_TARGETS_SQL, retry_before)
    return [dict(row) for row in rows]


async def save_description_facts(
    conn,
    facts: Iterable[
        tuple[str, int | None, bool, bool, str | None, Any, Any, bool]
    ],
) -> int:
    """Persist (canonical_id, min_experience, is_pm_role, fetch_failed, work_mode,
    salary_min, salary_max, salary_checked).

    The salary pair is typed `Any` rather than `Decimal | None` for one reason:
    asyncpg binds a NUMERIC from a Decimal and the SQLite test shim stores a REAL
    from a float, and both are legitimate callers of this function. Narrowing the
    annotation to Decimal would describe production and exclude the mirror the
    whole suite runs against — the accommodation Preferences._as_tuple already
    makes one module over.

    Returns how many rows were handed to the database. Not in a transaction, and
    it does not need to be: each row is independent, every write is idempotent,
    and a run that dies halfway leaves the jobs it did finish correctly marked
    rather than making the next run redo them.

    This is also the one write the digest performs BEFORE a send, which is a
    deliberate exception to this module's usual ordering. It is safe because it
    touches nothing about the job's lifecycle: no notification row, no receipt. A
    send that fails afterwards leaves these rows enriched and still queued,
    which is exactly what should happen — the facts are true regardless of
    whether an email went out, and re-reading the descriptions to rediscover
    them would be the only cost of not writing them here.
    """
    records = [
        (cid, years, bool(is_pm), bool(failed), mode, low, high, bool(checked))
        for cid, years, is_pm, failed, mode, low, high, checked in facts
    ]
    if not records:
        return 0

    await conn.executemany(SAVE_DESCRIPTION_FACTS_SQL, records)
    return len(records)


async def record_sent(conn, user_id: Any, canonical_ids: Iterable[str], digest: str) -> int:
    """Record that this user has now been emailed about exactly these jobs.

    Call this only after a send has succeeded. Returns the number of jobs now
    recorded as notified to this user, which the caller compares against what it
    actually emailed.

    TWO WRITES, ONE TRANSACTION.

      1. user_job_notifications  — the per-user record. THE authority: this is
         what fetch_candidates reads, so this is what stops a re-send.
      2. notifications           — the sent-log receipt, per job and per digest.

    They share one transaction because either of them without the other is a
    distinct failure: a record with no receipt is a send nobody can explain, and
    a receipt with no record re-sends the job tomorrow.

    THERE WAS A THIRD WRITE UNTIL PHASE 1g: jobs.notified, the old global
    boolean. 1d moved the read authority to the table above and deliberately kept
    writing the boolean as a rollback net — so reverting 1d could not re-send
    anything that went out during the 1d window, so the backfill could be re-run
    against an accurate source, and so the dashboard's badge stayed honest with no
    TypeScript changing. 1e made the queue read jobs.seeded instead, 1f moved the
    badge onto this table, and by 1g nothing read the boolean at all. It was
    dropped rather than left: a column written by one statement and read by none
    is a rollback net for a rollback nobody can still perform, and the next person
    to find it would reasonably assume something depends on it.

    The count returned is the per-user one, deliberately, because that is the
    table that decides. It counts GENUINE inserts, so re-recording a job this
    user already knew about contributes nothing — which is what makes a caller's
    "emailed N but recorded M" warning mean something.
    """
    ids = list(canonical_ids)
    if not ids:
        return 0

    async with conn.transaction():
        recorded = await record_notified(conn, user_id, ids)
        await conn.executemany(INSERT_NOTIFICATION_SQL, [(cid, digest) for cid in ids])

    return recorded
