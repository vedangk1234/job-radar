import { sql } from "./db";
import { isMatch, MAX_MIN_EXPERIENCE, type Preferences } from "./match";

/**
 * The jobs queries. Read-only, everywhere — the crawler owns this table, and a
 * dashboard that could edit `notified` or `status` would be able to silently
 * un-send or re-send a digest. Nothing here writes.
 *
 * Both screens read jobs one company at a time, because both screens are the
 * same collapsible list and a collapsed row needs no jobs at all. The flat
 * "every job, newest first" dump this file used to serve is gone: 788 rows in a
 * table nobody could scan was a worse answer to "what should I apply to" than
 * twelve rows that expand.
 */

export type JobRow = {
  canonical_id: string;
  company_id: number;
  title: string;
  city: string | null;
  location: string | null;
  url: string;
  status: string;
  notified: boolean;
  min_experience: number | null;
  is_pm_role: boolean;
  desc_checked: boolean;
  desc_fetch_failed: boolean;
  first_seen_at: string;
};

/**
 * The columns every job list needs. Deliberately no description — there is no
 * column, on purpose; see the comment above `jobs` in db/schema.sql.
 *
 * A function rather than a constant because a postgres.js fragment is a Query
 * object, and one shared instance embedded in two statements is a needless
 * question about whether that object carries state. A new one per call costs
 * nothing and asks nothing.
 */
const jobColumns = () => sql`
  j.canonical_id, j.company_id, j.title, j.city, j.location, j.url,
  j.status, j.notified, j.min_experience, j.is_pm_role, j.desc_checked,
  j.desc_fetch_failed, j.first_seen_at
`;

/**
 * Every OPEN job for one company, unfiltered — all seniorities, all roles,
 * sales and HR included. This is what the raw Jobs view expands to.
 *
 * Closed jobs are left out on both screens. A posting that has closed is not
 * something to apply to, and keeping them would make the count on the row mean
 * "jobs we have ever seen here", which is a fact about the crawler rather than
 * about the company.
 */
export async function openJobsForCompany(companyId: number): Promise<JobRow[]> {
  return sql<JobRow[]>`
    SELECT ${jobColumns()}
      FROM jobs j
     WHERE j.company_id = ${companyId}
       AND j.status = 'open'
     ORDER BY j.first_seen_at DESC, j.title ASC
  `;
}

/**
 * The rows that could possibly match, for one company or for all of them.
 *
 * This is the cheap half of notify/match.is_match pushed into SQL, and it is
 * only the half that translates without an argument:
 *
 *   status = 'open'                          the lifecycle test
 *   city = ANY(preferences.cities)           city_ok
 *   is_pm_role = FALSE                       NOT pm_excluded
 *   min_experience IS NULL OR <= ceiling     NOT experience_excluded
 *
 * The other three tests read the TITLE with word-boundary regexes and a level
 * detector that has to mask experience numbers first, and those stay in
 * lib/match.ts where they are tested and parity-checked. So the split is not
 * "some in SQL, some in TypeScript" for its own sake: it is every rule that
 * Postgres can state exactly, and no rule it would have to state approximately.
 *
 * What this is NOT is a `notified = FALSE` filter. Home answers "what fits me
 * right now", not "what has not been emailed yet" — a job already sent is still
 * a job worth applying to, and hiding it the morning after the digest would be
 * the dashboard disagreeing with the inbox for no reason.
 *
 * The volume this keeps off the wire is the point: ~790 open rows narrow to the
 * few hundred in the right cities before anything is decided, and the client
 * never sees a job row at all until a company is expanded.
 */
export async function matchCandidates(
  prefs: Preferences,
  companyId: number | null = null,
): Promise<JobRow[]> {
  const cities = [...prefs.cities];
  return sql<JobRow[]>`
    SELECT ${jobColumns()}
      FROM jobs j
     WHERE j.status = 'open'
       AND j.city = ANY(${cities}::text[])
       AND j.is_pm_role = FALSE
       AND (j.min_experience IS NULL OR j.min_experience <= ${MAX_MIN_EXPERIENCE})
       AND (${companyId}::int IS NULL OR j.company_id = ${companyId}::int)
     ORDER BY j.first_seen_at DESC, j.title ASC
  `;
}

/** The matching jobs for one company: the SQL prefilter, then the title rules. */
export async function matchingJobsForCompany(
  companyId: number,
  prefs: Preferences,
): Promise<JobRow[]> {
  const candidates = await matchCandidates(prefs, companyId);
  return candidates.filter((job) => isMatch(job, prefs));
}

/**
 * company_id -> how many of its open jobs match, for every company at once.
 *
 * One query and one pass, rather than a query per company: twelve companies is
 * twelve round trips to Tokyo, and the number only exists to be printed next to
 * a name.
 */
export async function matchCountsByCompany(prefs: Preferences): Promise<Map<number, number>> {
  const candidates = await matchCandidates(prefs);
  const counts = new Map<number, number>();
  for (const job of candidates) {
    if (!isMatch(job, prefs)) continue;
    counts.set(job.company_id, (counts.get(job.company_id) ?? 0) + 1);
  }
  return counts;
}
