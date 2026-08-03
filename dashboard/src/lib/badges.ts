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
 *   NEW        a match I have not been told about  (match && !user_notified)
 *   NOTIFIED   a match a digest has told me about  (match && user_notified)
 *   SUGGESTED  on the RAW list: this one fits      (match, on a screen that does
 *                                                   not filter by matching)
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
 * has already told me about it — NEW is the row I have not seen in my inbox.
 * On Jobs nothing is filtered, so the useful distinction is the opposite one:
 * which of these several hundred postings the filter would have picked. Showing
 * NEW/NOTIFIED there would put a badge on every row and mean nothing; showing
 * SUGGESTED on Home would put the same badge on every row and mean nothing.
 *
 * A NOTE ON WHAT YOU WILL ACTUALLY SEE TODAY
 * ------------------------------------------
 * Most matches read NOTIFIED, because most of the table is back catalogue: a
 * company's first-ever crawl stores its whole board silently (see "the silent
 * seed" in CLAUDE.md), and the 1d backfill recorded every job the old boolean
 * covered as already-notified for the one existing account.
 *
 * NEW is not speculative and has not been since the hourly cron started. A
 * genuinely new posting arrives with no row in user_job_notifications for
 * anybody, so it reads NEW on Home until that hour's digest sends and records
 * one. A job that somebody else's digest sent stays NEW here, which is the whole
 * point of the change.
 */

export type HomeBadge = "NEW" | "NOTIFIED";
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
  return job.user_notified
    ? {
        label: "NOTIFIED",
        title: "This job fits your filter and has already gone out to you in a digest email.",
      }
    : {
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
