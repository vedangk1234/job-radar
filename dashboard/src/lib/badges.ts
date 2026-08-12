/**
 * The one-word status badge on a job row. Derived at render, never stored.
 *
 * WHY THESE ARE NOT COLUMNS
 * -------------------------
 * A badge is a reading of two facts that already exist — whether the job passes
 * the filter, and whether it has been emailed. Storing the reading would mean
 * storing an answer whose inputs can change without the job changing at all: the
 * `preferences` row is editable from another tab, so a stored SUGGESTED would be
 * stale the moment a keyword was removed, and stale in the direction that hides
 * work rather than showing it. Deriving it costs one boolean per row and cannot
 * go out of date, because there is nothing to keep up to date.
 *
 * It is also why there is no migration here and no backfill: adding a badge
 * changed no data, only what the screen says about data it already had.
 *
 *   NEW        a match not yet emailed to me      (match && !user_notified)
 *   (none)     a match already handled             (match && user_notified)
 *   SUGGESTED  on the RAW list: this one fits      (match, on a screen that does
 *                                                   not filter by matching)

 *
 * THERE WAS A `NOTIFIED` BADGE ON THAT SECOND LINE AND THE FILTER-FIRST SLICE
 * REMOVED IT, WHICH IS A CHANGE OF HONESTY RATHER THAN OF DESIGN.
 *
 * It read "already emailed to you", and for two phases that was true: a row in
 * user_job_notifications was written by record_sent, after Resend returned an id.
 * The table has always held a second kind of row — written because a job must
 * NEVER be emailed to that person — but they were rare: the 1d backfill, and a
 * company's history when somebody first watched it.
 *
 * Filter-first makes the second kind the COMMON kind. A brand-new account's first
 * save records every back-catalogue job its filter matches as already-seen (see
 * lib/preferences.suppressFilterBacklog), so on day one that account's Home would
 * have been a wall of badges claiming emails it had never received. The badge
 * would have been lying on almost every row of the screen a person sees first.
 *
 * It could not be caught by looking, either, which is why it came out in the same
 * slice rather than later: the operator's own rows are genuine receipts, so their
 * screen would have looked exactly as it always had.
 *
 * SO THE ABSENCE OF A BADGE NOW MEANS "handled" — sent, or suppressed, and the row
 * itself does not distinguish them because the queue does not either. NEW keeps
 * its exact meaning, which is the one worth having: this is in your digest queue
 * and has not gone out yet. Deriving it is unchanged — `match && !user_notified`,
 * the same predicate from the same table.
 *
 * "TOLD ME", NOT "TOLD SOMEBODY" — THE PHASE 1f CHANGE
 * ----------------------------------------------------
 * Until 1f the first two read `jobs.notified`, a single boolean on the job. It
 * means "somebody was emailed about this", which is a faithful paraphrase of
 * "I was emailed about this" for exactly as long as there is one account. With
 * two, the first person's digest would turn the second person's NEW into
 * NOTIFIED — a badge asserting an email that person never got, which is the
 * flattening `user_job_notifications` was created in 1d to end and which the
 * digest queue stopped doing in 1e. The screen was the last reader left.
 *
 * So `user_notified` now comes from lib/jobs.notifiedToUser: does a row exist in
 * user_job_notifications for THIS user and this job. Same table, same predicate
 * and same meaning as notify/store.CANDIDATES_SQL's NOT EXISTS — the digest asks
 * it to decide what to send, this asks it to decide what to say.
 *
 * For the one real account the two readings cover the identical set of jobs (the
 * 1d backfill wrote a row for every notified = TRUE job; record_sent has written
 * both ever since), so this changed no badge on any screen. That is not an
 * assumption: `npm run badge-check` compares the two, job by job, against the
 * live database and fails on any difference.
 *
 * WHY HOME AND JOBS SHOW DIFFERENT BADGES
 * ---------------------------------------
 * They are answers to different questions. On Home every row is a match, so
 * "does it match" is not news and the useful distinction is whether the digest
 * has already handled it — NEW is the row still queued for my inbox. On Jobs
 * nothing is filtered, so the useful distinction is the opposite one: which of
 * these several hundred postings the filter would have picked. Showing NEW there
 * would put a badge on almost every matching row and mean nothing; showing
 * SUGGESTED on Home would put the same badge on every row and mean nothing.
 *
 * A NOTE ON WHAT YOU WILL ACTUALLY SEE TODAY
 * ------------------------------------------
 * Most matches carry NO badge, because most of the table is already handled: a
 * company's first-ever crawl stores its whole board silently (see "the silent
 * seed" in CLAUDE.md), the 1d backfill recorded every job the old boolean
 * covered, and a filter save records the back catalogue it admits.
 *
 * NEW is not speculative and has not been since the hourly cron started. A
 * genuinely new posting arrives with no row in user_job_notifications for
 * anybody, so it reads NEW on Home until that hour's digest sends and records
 * one. A job that somebody else's digest sent stays NEW here, which is the whole
 * point of the per-user predicate.
 */

export type HomeBadge = "NEW";
export type JobsBadge = "SUGGESTED";
export type Badge = HomeBadge | JobsBadge;

/** What the badge says, and the tooltip that explains why it says it. */
export type BadgeInfo = { label: Badge; title: string };

/**
 * The two derived facts a badge is read from — neither of them a column.
 *
 * Structural rather than importing JobView, so this module stays free of the
 * database import graph and can be called from a check script as easily as from
 * a client component.
 */
type Judged = { user_notified?: boolean | null; matches?: boolean | null };

/**
 * The badge for a row on Home, where every row is already a match.
 *
 * `matches` is still consulted rather than assumed. The screen guarantees it
 * today, but a function that reads "NEW = a match I have not been told about"
 * and checks only half of that is a function that will one day label a non-match
 * NEW on a screen nobody has written yet.
 */
export function homeBadge(job: Judged): BadgeInfo | null {
  if (job.matches === false) return null;
  // Already handled — emailed, or recorded as history by a filter save. No badge,
  // because the row cannot tell those two apart and neither can the queue. See the
  // block comment above for why claiming "already emailed" here became a lie.
  if (job.user_notified) return null;
  return {
    label: "NEW",
    title:
      "This job fits your filter and has not been emailed to you yet — it is sitting in " +
      "your digest queue.",
  };
}

/**
 * The badge for a row on the raw Jobs list: SUGGESTED, or nothing at all.
 *
 * Nothing at all is the common case and is the point. The Jobs screen exists to
 * show the sales roles and the VP openings alongside everything else, so a badge
 * on the handful that fit is a way to find them in a list that is not sorted by
 * whether they fit.
 */
export function jobsBadge(job: Judged): BadgeInfo | null {
  if (!job.matches) return null;
  return {
    label: "SUGGESTED",
    title:
      "This posting passes the same filter Home uses — city, keyword, no excluded word, " +
      "no level above your ceiling, and nothing the description ruled out.",
  };
}

/** CSS modifier for a badge, so the stylesheet has one class per meaning. */
export function badgeClass(label: Badge): string {
  return `badge ${label.toLowerCase()}`;
}
