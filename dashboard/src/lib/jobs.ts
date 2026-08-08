import { sql } from "./db";
import { isMatch, type Preferences } from "./match";

/**
 * The jobs queries.
 *
 * WHAT THE DASHBOARD MAY WRITE HERE, AND WHY IT IS EXACTLY TWO COLUMNS
 * --------------------------------------------------------------------
 * This file used to be read-only in full, and the reason still holds for every
 * column except two: the crawler owns `jobs`, and a dashboard that could edit
 * its lifecycle columns would be able to silently un-send or re-send a digest.
 * That rule is about columns the crawler DERIVES — it observed the posting, so
 * it is the only thing entitled to say when the posting appeared, whether it is
 * still open, or whether it has been emailed.
 *
 * `applied` / `applied_at` are the other kind of fact. They record something no
 * fetch can discover and no crawl can recompute: a person applied to this job,
 * at this moment. There is nowhere else they could be written from, and the
 * crawler correspondingly never writes them — crawler/store.UPSERT_SEEN and
 * notify/store.SAVE_DESCRIPTION_FACTS_SQL name neither column, which
 * tests/test_applied_column.py asserts both textually and by ticking a row and
 * running whole crawl cycles over it.
 *
 * So the split is ownership, not read-versus-write: the crawler owns the
 * lifecycle, the dashboard owns the one column about me. `setApplied` below is
 * the only statement in this app that writes to `jobs`, and it names those two
 * columns and nothing else.
 *
 * Both screens read jobs one company at a time, because both screens are the
 * same collapsible list and a collapsed row needs no jobs at all. The flat
 * "every job, newest first" dump this file used to serve is gone: 788 rows in a
 * table nobody could scan was a worse answer to "what should I apply to" than
 * twelve rows that expand.
 *
 * THE THREE-STEP TYPE LADDER, AND WHY IT IS THREE STEPS
 * -----------------------------------------------------
 * A row on a screen carries three different kinds of fact, and Phase 1f is the
 * step that made the middle one exist:
 *
 *   JobRow      the columns of `jobs`. The same answer for everybody, forever.
 *   UserJobRow  + one fact about ME: have I been told about this job?
 *   JobView     + the filter's verdict, evaluated against MY preferences now.
 *
 * Only the first is stored. The second is a row in `user_job_notifications`
 * that either exists or does not; the third is isMatch() run at render time.
 * Keeping them as separate types is what stops the second and third from
 * drifting back into the table, which is the mistake `jobs.notified` was.
 */

export type JobRow = {
  canonical_id: string;
  company_id: number;
  title: string;
  city: string | null;
  location: string | null;
  url: string;
  status: string;
  min_experience: number | null;
  is_pm_role: boolean;
  desc_checked: boolean;
  desc_fetch_failed: boolean;
  first_seen_at: string;
  applied: boolean;
  applied_at: string | null;
};

/**
 * A job row plus "have I been emailed about this one" — the second rung.
 *
 * `user_notified` is NOT a column and is deliberately not called `notified`.
 * Until Phase 1f the boolean `jobs.notified` sat in the row and meant "SOMEBODY
 * was emailed about this", which was an adequate paraphrase of "I was emailed
 * about this" only while the install had one user. Phase 1d gave that fact an
 * owner in `user_job_notifications`, and this is the dashboard reading it: the
 * same per-user truth the digest queue asks about in notify/store.CANDIDATES_SQL,
 * asked from the other side.
 *
 * The rename is load-bearing rather than cosmetic. A field still called
 * `notified` would let a reader — or an edit — carry the old global meaning into
 * a per-user screen without noticing, and it is what lets
 * tests/test_dashboard_notified.py forbid the bare substring `.notified` across
 * this whole app instead of maintaining an allowlist of near-misses.
 */
export type UserJobRow = JobRow & { user_notified: boolean };

/**
 * A job row plus the one thing about it that is not a column: whether it passes
 * the digest filter.
 *
 * It is not a column and must not become one. `matches` is isMatch() evaluated
 * at render time against the preferences as they are right now, so editing the
 * filter changes every badge on the next page load with nothing to backfill. A
 * stored flag would be a cached answer to a question whose inputs the user can
 * edit from another tab — wrong within seconds and wrong silently.
 *
 * On the matches screen this is true by construction. On the raw Jobs screen it
 * is the SUGGESTED badge: the same shared filter, applied to a list that is not
 * filtered by it.
 */
export type JobView = UserJobRow & { matches: boolean };

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
  j.status, j.min_experience, j.is_pm_role, j.desc_checked,
  j.desc_fetch_failed, j.first_seen_at, j.applied, j.applied_at
`;

/**
 * "Has this user been told about this job?" — THE one definition, in SQL.
 *
 * The mirror of the digest's own clause in notify/store.CANDIDATES_SQL, asked
 * from the other side: the digest asks NOT EXISTS to decide what to send, this
 * asks EXISTS to decide what to label. Two readings of one table, and no third
 * place where the rule could be restated slightly differently.
 *
 * A ROW EXISTING IS THE WHOLE DECISION. There is no flag on it and no state in
 * between — see the table's comment block in db/schema.sql. Note what that means
 * for the two kinds of row it holds: most are receipts from a real send, and the
 * rest were written because the job must never be emailed to that person (the 1d
 * backfill, and db/watchlist's history suppression when a company is first
 * tracked). Both say the same thing to this query, which is the same thing they
 * say to the queue: this person will not be told about this job.
 *
 * WHAT THIS REPLACED. `j.notified` used to travel on every row above, and
 * lib/badges.homeBadge read it to choose NEW or NOTIFIED. That boolean says
 * "somebody was emailed", so the moment a second account exists the first
 * person's digest would silence a job on the second person's screen — the same
 * flattening the digest queue removed in 1d, arriving one layer later. This is
 * also why nothing in the dashboard reads the column any more, which is the
 * precondition Phase 1g's DROP depends on.
 *
 * Exported so that scripts/badge-check.ts can run the production predicate under
 * two identities rather than restating it and proving a copy.
 */
export function notifiedToUser(userId: string) {
  return sql`
    EXISTS (
      SELECT 1
        FROM user_job_notifications n
       WHERE n.user_id = ${userId}
         AND n.canonical_id = j.canonical_id
    ) AS user_notified
  `;
}

/**
 * Every OPEN job for one company, unfiltered — all seniorities, all roles,
 * sales and HR included. This is what the raw Jobs view expands to.
 *
 * Closed jobs are left out on both screens. A posting that has closed is not
 * something to apply to, and keeping them would make the count on the row mean
 * "jobs we have ever seen here", which is a fact about the crawler rather than
 * about the company.
 *
 * The user is PASSED IN, since Phase 2a — it comes from the session by way of
 * lib/guard.requireSession(). It used to be resolved here through the MY_EMAIL
 * bridge, which answered "whose install is this" when the question the row
 * actually asks is "who is reading this page".
 */
export async function openJobsForCompany(
  companyId: number,
  userId: string,
): Promise<UserJobRow[]> {
  return sql<UserJobRow[]>`
    SELECT ${jobColumns()},
           ${notifiedToUser(userId)}
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
 *   the experience clause below               NOT experience_excluded
 *
 * The other three tests read the TITLE with word-boundary regexes and a level
 * detector that has to mask experience numbers first, and those stay in
 * lib/match.ts where they are tested and parity-checked. So the split is not
 * "some in SQL, some in TypeScript" for its own sake: it is every rule that
 * Postgres can state exactly, and no rule it would have to state approximately.
 *
 * What this is NOT is a "has it been emailed yet" filter, in either the old
 * global form or the per-user one. Home answers "what fits me right now", not
 * "what has not been emailed yet" — a job already sent is still a job worth
 * applying to, and hiding it the morning after the digest would be the dashboard
 * disagreeing with the inbox for no reason. Being told about it is carried on
 * the row as `user_notified` and shown as a badge; it decides nothing here.
 *
 * The volume this keeps off the wire is the point: ~790 open rows narrow to the
 * few hundred in the right cities before anything is decided, and the client
 * never sees a job row at all until a company is expanded.
 */
export async function matchCandidates(
  prefs: Preferences,
  userId: string,
  companyId: number | null = null,
): Promise<UserJobRow[]> {
  const cities = [...prefs.cities];
  return sql<UserJobRow[]>`
    SELECT ${jobColumns()},
           ${notifiedToUser(userId)}
      FROM jobs j
     WHERE j.status = 'open'
       AND j.city = ANY(${cities}::text[])
       AND j.is_pm_role = FALSE
       AND ${experienceClause(prefs)}
       AND (${companyId}::int IS NULL OR j.company_id = ${companyId}::int)
     ORDER BY j.first_seen_at DESC, j.title ASC
  `;
}

/**
 * NOT experience_excluded, in SQL — the transcription of
 * lib/match.experienceExcluded, branch for branch.
 *
 * IT MUST BE EXACTLY AS WIDE AS THE MATCHER AND NEVER NARROWER. This clause is a
 * prefilter: whatever it drops, lib/match.isMatch never sees, so a clause tighter
 * than the rule silently deletes matches and the "for you" count is quietly wrong
 * about what the digest would send. That is why it is three explicit branches
 * rather than one clever expression.
 *
 * BRANCHED IN TYPESCRIPT RATHER THAN IN SQL, and this is the load-bearing part.
 * The tempting one-liner is
 *
 *     (j.min_experience IS NULL AND ${prefs.show_no_experience_jobs})
 *      OR j.min_experience BETWEEN ${low} AND ${high}
 *
 * which is correct today and correct BY ACCIDENT in the unconfigured case:
 * `BETWEEN NULL AND NULL` evaluates to NULL, NULL is not TRUE, so the row is
 * excluded — the right answer reached through three-valued logic that nobody
 * reading it would have checked. A rule that holds by accident is one a later
 * rewrite breaks without failing anything. So the range being unset is its own
 * branch, saying FALSE out loud.
 *
 * Note also that BETWEEN already excludes NULL, which is why the toggle-off branch
 * needs no `IS NOT NULL`: a NULL min_experience simply does not satisfy it.
 */
function experienceClause(prefs: Preferences) {
  const { exp_range_low: low, exp_range_high: high } = prefs;

  // Unconfigured range -> nothing matches. See notify/match.experience_excluded
  // for why this is not a fallback to the install's 0..2, and why an onboarded
  // account can never be in this state.
  if (low === null || high === null) return sql`FALSE`;

  if (prefs.show_no_experience_jobs) {
    return sql`(j.min_experience IS NULL OR j.min_experience BETWEEN ${low} AND ${high})`;
  }
  return sql`j.min_experience BETWEEN ${low} AND ${high}`;
}

/** The matching jobs for one company: the SQL prefilter, then the title rules. */
export async function matchingJobsForCompany(
  companyId: number,
  prefs: Preferences,
  userId: string,
): Promise<JobView[]> {
  const candidates = await matchCandidates(prefs, userId, companyId);
  // `matches: true` is not a claim being made twice: these rows are the output
  // of isMatch, so the field is the filter's verdict carried forward rather than
  // a second opinion about it. The Jobs screen computes the same field the same
  // way over a list that was never filtered.
  return candidates.filter((job) => isMatch(job, prefs)).map((job) => ({ ...job, matches: true }));
}

/**
 * Every open job for one company, each labelled with whether it also matches.
 *
 * This is what the raw Jobs screen expands to, and the label is the SUGGESTED
 * badge. Computed here rather than in the browser for one reason: the filter is
 * lib/match.ts run against the live `preferences` row, and shipping both to the
 * client to re-derive a boolean per row would be sending the rule and its inputs
 * in order to avoid sending the answer.
 */
export async function openJobsWithMatchFlag(
  companyId: number,
  prefs: Preferences,
  userId: string,
): Promise<JobView[]> {
  const jobs = await openJobsForCompany(companyId, userId);
  return jobs.map((job) => ({ ...job, matches: isMatch(job, prefs) }));
}

/** What one company's row prints next to its name, in one pass over the matches. */
export type CompanyJobCounts = { match: number; applied: number };

/**
 * company_id -> its match count and how many of those are already applied to.
 *
 * One query and one pass, rather than a query per company: twelve companies is
 * twelve round trips to Tokyo, and the numbers only exist to be printed next to
 * a name.
 *
 * The applied count is deliberately counted over MATCHES rather than over every
 * open job. It sits beside the "for you" number and has to mean a share of it —
 * "3 of your 14" — rather than being a total from a different population that
 * could exceed the number next to it and read as a bug.
 */
export async function matchCountsByCompany(
  prefs: Preferences,
  userId: string,
): Promise<Map<number, CompanyJobCounts>> {
  const candidates = await matchCandidates(prefs, userId);
  const counts = new Map<number, CompanyJobCounts>();
  for (const job of candidates) {
    if (!isMatch(job, prefs)) continue;
    const entry = counts.get(job.company_id) ?? { match: 0, applied: 0 };
    entry.match += 1;
    if (job.applied) entry.applied += 1;
    counts.set(job.company_id, entry);
  }
  return counts;
}

/**
 * Tick or untick "I applied to this". The only write this app makes to `jobs`.
 *
 * Two columns, named explicitly, and no other column in the statement — the
 * same discipline notify/store.SAVE_DESCRIPTION_FACTS_SQL follows in the other
 * direction. It cannot reopen a closed job, cannot restamp first_seen_at, and
 * cannot touch anything the crawler owns.
 *
 * IT RETURNS A JobRow, NOT A JobView, AND THAT IS THE POINT. The columns are all
 * this statement knows about: it does not evaluate the filter and it does not
 * ask whether you have been emailed about this job, because neither is a column
 * and a write has no business deriving them. Keeping `notifiedToUser` out of the
 * RETURNING is what lets "the one write in this app names two columns and
 * nothing else" stay true by reading the statement. The client re-attaches
 * `matches` and `user_notified` from the row it already had — see
 * CompanyList.toggleApplied, where the reason is the same for both.
 *
 * `applied_at` is COALESCEd rather than overwritten, so the timestamp records
 * when the box was FIRST ticked and a repeated PATCH is genuinely idempotent
 * instead of quietly resetting the date. Unticking clears it to NULL, so the
 * pair can never say "not applied, on the 3rd of August" — and a later re-tick
 * therefore gets an honest new stamp rather than resurrecting the old one.
 *
 * Returns the re-read row so the client renders what the database now says
 * rather than what the click assumed, exactly as PATCH /api/companies/:id does.
 * A canonical_id that does not exist returns null and the route answers 404.
 */
export async function setApplied(canonicalId: string, applied: boolean): Promise<JobRow | null> {
  const [row] = await sql<JobRow[]>`
    UPDATE jobs AS j
       SET applied    = ${applied},
           applied_at = CASE WHEN ${applied} THEN COALESCE(j.applied_at, now()) ELSE NULL END
     WHERE j.canonical_id = ${canonicalId}
    RETURNING ${jobColumns()}
  `;
  return row ?? null;
}
