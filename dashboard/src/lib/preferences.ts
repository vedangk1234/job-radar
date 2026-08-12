import { sql } from "./db";
import { CITY_LABELS, type Preferences } from "./labels";
import { isMatch, type Preferences as MatchPreferences } from "./match";

/**
 * The current user's preferences row — the filter that decides what reaches an
 * inbox.
 *
 * Keyed on the OWNER, not on the old fixed address. This used to say
 * `WHERE id = 1`, which was correct while db/schema.sql pinned the table to one
 * row and nothing in the database named a user; preferences.user_id replaced
 * that assumption with an owner. The owner arrives as an argument: 2a threaded
 * the session's public.users id down from lib/guard.ts and 2b removed the
 * ambient MY_EMAIL lookup that used to supply it, so whose row this is is now
 * decided by the request rather than by the deployment's environment.
 *
 * Server-only — it imports the database. The constants and types live in
 * lib/labels.ts so the editor component can use them without dragging
 * postgres.js into the browser bundle.
 */

export {
  CITY_LABELS,
  CITY_LABEL_GROUPS,
  CITY_UMBRELLAS,
  KEYWORD_CAP,
  LEVEL_LABELS,
  SELECTABLE_CITY_LABELS,
  type Preferences,
} from "./labels";

/**
 * The two ways this can find nothing, and why they must not share a message.
 *
 * "This user has no row" is one thing. "db/schema.sql has been applied, adding
 * a nullable user_id, but scripts/backfill_preferences_user.py has not run yet"
 * is a completely different thing — the row is right there, unowned, and a
 * message telling you to re-seed would be advice that overwrites the filter you
 * are trying to recover. So the miss path asks one more question before it
 * decides what to say. Mirrors notify/store.load_preferences, which faces the
 * same fork and (because it has a defaults fallback to protect) refuses in the
 * same case.
 */
async function explainMissingRow(): Promise<never> {
  const [{ unowned }] = await sql<{ unowned: number }[]>`
    SELECT count(*)::int AS unowned FROM preferences WHERE user_id IS NULL
  `;

  if (unowned > 0) {
    throw new Error(
      "preferences.user_id is unset on an existing row: db/schema.sql has been " +
        "applied but scripts/backfill_preferences_user.py has not run. Run it — " +
        "the filter is still there, it just has no owner yet.",
    );
  }
  throw new Error(
    "the preferences row is missing. Re-run `python scripts/init_db.py` to seed it.",
  );
}

/**
 * ELEVEN COLUMNS SINCE THE WORK-MODE SLICE, and the new ones split two ways.
 *
 * exp_range_low, exp_range_high and show_no_experience_jobs are read by the RULES
 * (lib/match.experienceExcluded) as well as shown on the screen.
 * years_of_experience_held is read by the SCREEN ONLY — it is how much experience
 * the person has, it never filters, and lib/match.Preferences has no field for it
 * so no rule can reach it. Selected here because the editor has to render it.
 *
 * Still an explicit column list, as Step 0 warned: a SELECT * over this table
 * would drag the five columns no feature step has wired up into a type that
 * describes a filter.
 */
export async function loadPreferences(userId: string): Promise<Preferences> {
  const [row] = await sql<Preferences[]>`
    SELECT cities, selected_cities, all_india,
           keywords, exclusions, max_level,
           exp_range_low, exp_range_high, years_of_experience_held,
           show_no_experience_jobs, work_modes, salary_bands,
           updated_at, onboarding_completed
      FROM preferences
     WHERE user_id = ${userId}
  `;
  if (!row) return explainMissingRow();
  return row;
}

/**
 * Has this person chosen their own filter yet? The whole of what the onboarding
 * gate in lib/guard.requirePage reads.
 *
 * ONE COLUMN, ONE INDEXED LOOKUP, and it is a separate statement rather than a
 * join bolted onto lib/link-user.ts branch 1. That query is the steady-state
 * path of every authenticated request and would have carried the flag for free —
 * but linkUser is documented as the single highest-risk function in the app, its
 * three branches are the thing a reader has to hold in their head, and
 * requireSession() (every API route) has no use for the answer. The read costs
 * one hit on preferences_one_per_user_idx, on page renders only.
 *
 * NO ROW MEANS FALSE, WHICH SENDS THEM TO ONBOARDING RATHER THAN PAST IT. That
 * is the closed default, and it is also the honest one: an account with no
 * preferences row has certainly not chosen a filter. It does not paper over the
 * missing row — every screen calls loadPreferences immediately afterwards and
 * that is where the two-way diagnosis above lives. This function's job is to
 * decide a redirect, so it answers the question it was asked and leaves the
 * explaining to the loader.
 */
export async function onboardingCompleted(userId: string): Promise<boolean> {
  const [row] = await sql<{ onboarding_completed: boolean }[]>`
    SELECT onboarding_completed FROM preferences WHERE user_id = ${userId}
  `;
  return row?.onboarding_completed ?? false;
}

/**
 * Replace this user's preferences row wholesale.
 *
 * The UPDATE is scoped by user_id, so it can only ever touch the caller's own
 * filter — under the old `WHERE id = 1` that was true by there being only one
 * row, which is a property of the schema rather than of this statement.
 *
 * WHY THIS SETS onboarding_completed = TRUE AND NEVER READS IT. The column
 * arrived empty in Phase 3 Step 0 with no writer, and this is the writer. It
 * means "this person has chosen their own filter", and saving from this screen
 * is exactly that act — there is no separate ceremony to complete, and a
 * one-way flag needs no branch to set it. It is written unconditionally rather
 * than only when FALSE: an idempotent assignment has no state to get wrong, and
 * a guarded one would leave the column looking like it recorded WHEN somebody
 * onboarded, which it does not.
 *
 * The column list on the RETURNING is explicit, as it has been since Step 0
 * warned that a SELECT * over this table would drag eight unread onboarding
 * columns into a type that describes a filter.
 *
 * THE RANGE PARAMETERS ARE `number`, NOT `number | null`, AND THAT IS THE
 * INVARIANT MADE STRUCTURAL. An unconfigured range means "matches nothing"
 * (lib/match.experienceExcluded), so an ONBOARDED account must never have one —
 * and this is the only statement that writes either column or
 * onboarding_completed, so "onboarded implies configured" holds if and only if
 * this function cannot write a NULL. The type is what says it cannot; the route
 * above it rejects the request with a reason a person can read; and
 * `npm run onboarding-check` asserts the result against the live database. Three
 * layers, because the failure would be a silently empty digest.
 *
 * years_of_experience_held IS nullable here, deliberately. It never filters, so
 * "not stated" is a legitimate permanent answer rather than a broken filter.
 *
 * `cities` IS WRITTEN ALONGSIDE selected_cities, AND IT IS VESTIGIAL ON PURPOSE
 * FOR EXACTLY ONE SLICE. Cities Slice B moved the gate onto selected_cities +
 * all_india in both matchers; the DEPLOYED digest is still running the previous
 * commit, which reads `cities`. Dual-writing is what makes this slice a no-op
 * against production during the window between the commit and the deploy, and
 * what makes reverting it a no-op too — the same expand-then-contract ordering
 * jobs.notified followed through 1d-1g, where the write outlived its last reader
 * by design rather than by neglect.
 *
 * The route above derives it: the expanded selection, or the 25 India labels when
 * all_india is on, so an all-India account rolled back onto the old matcher gets
 * the closest honest approximation rather than an empty digest. The CONTRACT slice
 * removes the parameter, the column and this paragraph together.
 */
export async function savePreferences(
  userId: string,
  next: {
    cities: string[];
    selected_cities: string[];
    all_india: boolean;
    keywords: string[];
    exclusions: string[];
    max_level: number;
    exp_range_low: number;
    exp_range_high: number;
    years_of_experience_held: number | null;
    show_no_experience_jobs: boolean;
    work_modes: string[];
    salary_bands: string[];
  },
): Promise<SaveResult> {
  return sql.begin(async (tx) => {
    // The filter as it stands, read INSIDE the transaction and before the write.
    // Outside it, a concurrent save could land between the read and the UPDATE and
    // the delta would be computed against a filter that no longer existed.
    const [before] = await tx<Preferences[]>`
      SELECT cities, selected_cities, all_india,
             keywords, exclusions, max_level,
             exp_range_low, exp_range_high, years_of_experience_held,
             show_no_experience_jobs, work_modes, salary_bands,
             updated_at, onboarding_completed
        FROM preferences
       WHERE user_id = ${userId}
    `;
    if (!before) return explainMissingRow();

    const [row] = await tx<Preferences[]>`
      UPDATE preferences
         SET cities                   = ${next.cities}::text[],
             selected_cities          = ${next.selected_cities}::text[],
             all_india                = ${next.all_india},
             keywords                 = ${next.keywords}::text[],
             exclusions               = ${next.exclusions}::text[],
             max_level                = ${next.max_level},
             exp_range_low            = ${next.exp_range_low},
             exp_range_high           = ${next.exp_range_high},
             years_of_experience_held = ${next.years_of_experience_held},
             show_no_experience_jobs  = ${next.show_no_experience_jobs},
             work_modes               = ${next.work_modes}::text[],
             salary_bands             = ${next.salary_bands}::text[],
             onboarding_completed     = TRUE,
             updated_at               = now()
       WHERE user_id = ${userId}
      RETURNING cities, selected_cities, all_india,
                keywords, exclusions, max_level,
                exp_range_low, exp_range_high, years_of_experience_held,
                show_no_experience_jobs, work_modes, salary_bands,
                updated_at, onboarding_completed
    `;
    if (!row) return explainMissingRow();

    const suppressed = await suppressFilterBacklog(tx, userId, before, row);
    return { preferences: row, suppressed };
  });
}

/** What a save did: the row it wrote, and the history that row admitted. */
export type SaveResult = { preferences: Preferences; suppressed: number };

/**
 * Record what a NEW OR WIDENED filter has just admitted as already-notified.
 *
 * THE PORT OF notify/store.suppress_filter_backlog, and the obligation that used
 * to belong to tracking a company. Read that function first — the argument is
 * stated there in full and is not restated here, for the reason lib/watchlist.ts
 * gives about its own port: a rule written out twice drifts twice.
 *
 * The short form. The digest queue used to open with `EXISTS (user_companies …)`,
 * so watching a company admitted its whole stored history to somebody's queue and
 * lib/watchlist.watchCompany suppressed it in the same transaction. There is no
 * gate now, so a brand-new account matches the WHOLE CATALOG the instant it has a
 * filter, and a widening one admits every stored row the new answer covers. Same
 * archive-dump hazard, bigger blast radius, different trigger — so the suppression
 * hangs off the filter changing, which is here.
 *
 *     OLD  = what the row as it stands matches
 *     NEW  = what the row being saved matches
 *     suppress (NEW \ OLD)
 *
 * Day one is not a case in that rule, it FALLS OUT of it: a brand-new row is blank
 * on every field, and every blank in this app is fail-closed — no city answer
 * matches nothing, an empty work_modes matches nothing, an unconfigured range
 * matches nothing. So OLD is empty and the first save records the whole current
 * match set. Narrowing suppresses nothing; an unrelated edit suppresses nothing,
 * which is why this is a DELTA and not "everything matching right now" — the
 * latter would silently eat whatever arrived since the last digest every time
 * anybody touched the form.
 *
 * ═══ THREE THINGS THAT ARE DECISIONS RATHER THAN DETAILS ═══
 *
 * IT RUNS ON `tx`, NOT ON `sql`. A statement bound to the pool inside a
 * `sql.begin` callback runs on a DIFFERENT connection and is therefore outside the
 * transaction entirely — the one mistake in this shape that looks completely
 * correct. tests/test_watchlist_write.py asserts the binding textually, as it did
 * for watchCompany before it. Between the UPDATE and this there is a state in
 * which the filter is widened and the suppression is not written, and the hourly
 * digest runs in exactly that state if the process dies there.
 *
 * IT IS STATUS-BLIND, and that is the one clause that differs from the digest's
 * own. A closed job that reopens comes back `status = 'open'` with `miss_count = 0`
 * from crawler/store.UPSERT_SEEN while KEEPING its canonical_id — so suppressing
 * only open rows leaves a hole exactly one flap wide: the job sits closed and
 * unsuppressed while somebody saves, a vendor re-lists it a week later, and it
 * arrives as news. `reappeared` is the set crawler/diff.py most deliberately
 * refuses to treat as news. It DOES carry `seeded = FALSE`, because a seeded row
 * is excluded from every queue forever anyway and recording one would cost a row
 * per user per seeded job — 6,496 from a single sweep.
 *
 * THE REAL MATCHER DECIDES, NOT THE SQL PREFILTER. lib/jobs.ts's clauses are
 * allowed to be WIDER than isMatch, so using their shape here would suppress rows
 * that never matched — and those are exactly the rows enrichment can later turn
 * into genuine matches by filling a NULL min_experience. Over-suppressing today is
 * swallowing a real match tomorrow. So the candidate rows are judged by isMatch,
 * twice, and only the difference is written.
 */
async function suppressFilterBacklog(
  tx: typeof sql,
  userId: string,
  before: MatchPreferences,
  after: MatchPreferences,
): Promise<number> {
  // notify/store.SUPPRESSION_CANDIDATES_SQL, ported: the queue's own two clauses,
  // over jobs of any status. Only the columns isMatch reads, plus the key.
  const candidates = await tx<
    {
      canonical_id: string;
      title: string;
      city: string | null;
      work_mode: string | null;
      min_experience: number | null;
      is_pm_role: boolean;
      desc_checked: boolean;
      desc_fetch_failed: boolean;
      salary_min: string | null;
      salary_max: string | null;
    }[]
  >`
    SELECT j.canonical_id, j.title, j.city, j.work_mode,
           j.min_experience, j.is_pm_role, j.desc_checked,
           j.desc_fetch_failed, j.salary_min, j.salary_max
      FROM jobs j
     WHERE j.seeded = FALSE
       AND NOT EXISTS (
             SELECT 1
               FROM user_job_notifications n
              WHERE n.user_id = ${userId}
                AND n.canonical_id = j.canonical_id
           )
  `;

  const admitted = candidates
    .filter((job) => isMatch(job, after) && !isMatch(job, before))
    .map((job) => job.canonical_id);
  if (admitted.length === 0) return 0;

  /*
   * `unnest` RATHER THAN THE PYTHON'S CHUNKED MULTI-ROW VALUES, and the asymmetry
   * is forced rather than chosen. db/notifications.record_notified_sql builds
   * `VALUES ($1, $2), ($1, $3), …` in chunks of 400 because it has to stay inside
   * the subset Postgres and SQLite agree on — the whole pytest suite runs those
   * statements against the in-memory shim, which has no unnest. Nothing on this
   * side runs against SQLite, and one array parameter binds no per-row placeholder
   * at all, so the 65535-parameter ceiling and the chunking it forces both vanish.
   *
   * What the two DO share is every clause that decides anything: the table, the
   * column pair, `ON CONFLICT (user_id, canonical_id) DO NOTHING` so a re-save is
   * a genuine no-op, and `RETURNING canonical_id` so the count is rows actually
   * written rather than rows offered. tests/test_watchlist_write.py compares those
   * three rather than the whole statement, and says why.
   */
  const written = await tx<{ canonical_id: string }[]>`
    INSERT INTO user_job_notifications (user_id, canonical_id)
    SELECT ${userId}, unnest(${admitted}::text[])
    ON CONFLICT (user_id, canonical_id) DO NOTHING
    RETURNING canonical_id
  `;
  return written.length;
}

/**
 * Clean a user-supplied list: trim, lowercase, drop blanks, de-duplicate,
 * preserve order.
 *
 * Lowercasing is not cosmetic. notify/match.py compares against a normalised
 * title, so a keyword stored as "Python" would match nothing at all. Doing it
 * here means the stored value is the value that works.
 */
export function cleanList(input: unknown, { max = 200 }: { max?: number } = {}): string[] {
  if (!Array.isArray(input)) return [];
  const seen = new Set<string>();
  const out: string[] = [];
  for (const item of input) {
    const text = String(item ?? "")
      .replace(/\s+/g, " ")
      .trim()
      .toLowerCase();
    if (!text || seen.has(text)) continue;
    seen.add(text);
    out.push(text);
    if (out.length >= max) break;
  }
  return out;
}
