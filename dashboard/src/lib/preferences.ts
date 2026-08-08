import { sql } from "./db";
import { CITY_LABELS, type Preferences } from "./labels";

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

export { CITY_LABELS, KEYWORD_CAP, LEVEL_LABELS, type Preferences } from "./labels";

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

export async function loadPreferences(userId: string): Promise<Preferences> {
  const [row] = await sql<Preferences[]>`
    SELECT cities, keywords, exclusions, max_level, updated_at, onboarding_completed
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
 */
export async function savePreferences(
  userId: string,
  next: {
    cities: string[];
    keywords: string[];
    exclusions: string[];
    max_level: number;
  },
): Promise<Preferences> {
  const [row] = await sql<Preferences[]>`
    UPDATE preferences
       SET cities               = ${next.cities}::text[],
           keywords             = ${next.keywords}::text[],
           exclusions           = ${next.exclusions}::text[],
           max_level            = ${next.max_level},
           onboarding_completed = TRUE,
           updated_at           = now()
     WHERE user_id = ${userId}
    RETURNING cities, keywords, exclusions, max_level, updated_at, onboarding_completed
  `;
  if (!row) return explainMissingRow();
  return row;
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
