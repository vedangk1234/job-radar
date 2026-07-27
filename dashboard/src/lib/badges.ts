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
 *   NEW        a match nobody has emailed yet   (match && !notified)
 *   NOTIFIED   a match already in a digest      (match && notified)
 *   SUGGESTED  on the RAW list: this one fits   (match, on a screen that does
 *                                                not filter by matching)
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
 * Every stored match is currently notified = TRUE, because the whole table was
 * silently seeded (see "the silent seed" in CLAUDE.md) and no digest cron exists
 * yet. So Home reads NOTIFIED everywhere until the cron starts leaving fresh
 * jobs at notified = FALSE. NEW is not speculative — it is what the crawler
 * already writes for a genuinely new posting; there simply is not one yet.
 */

export type HomeBadge = "NEW" | "NOTIFIED";
export type JobsBadge = "SUGGESTED";
export type Badge = HomeBadge | JobsBadge;

/** What the badge says, and the tooltip that explains why it says it. */
export type BadgeInfo = { label: Badge; title: string };

type Judged = { notified?: boolean | null; matches?: boolean | null };

/**
 * The badge for a row on Home, where every row is already a match.
 *
 * `matches` is still consulted rather than assumed. The screen guarantees it
 * today, but a function that reads "NEW = a match that is not notified" and
 * checks only half of that is a function that will one day label a non-match
 * NEW on a screen nobody has written yet.
 */
export function homeBadge(job: Judged): BadgeInfo | null {
  if (job.matches === false) return null;
  return job.notified
    ? {
        label: "NOTIFIED",
        title: "This job fits your filter and has already gone out in a digest email.",
      }
    : {
        label: "NEW",
        title:
          "This job fits your filter and has not been emailed yet — it is sitting in the " +
          "digest queue (notified = false).",
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
