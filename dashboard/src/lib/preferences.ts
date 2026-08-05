import { sql } from "./db";
import { CITY_LABELS, type Preferences } from "./labels";

/**
 * The current user's preferences row — the filter that decides what reaches an
 * inbox.
 *
 * Keyed on the OWNER, not on the old fixed address. This used to say
 * `WHERE id = 1`, which was correct while db/schema.sql pinned the table to one
 * row and nothing in the database named a user; preferences.user_id replaced
 * that assumption with an owner. Callers still pass no id — the user is
 * resolved by lib/currentUser.ts, which is a Phase-2 bridge, so when auth lands
 * the change is confined to that file.
 *
 * Server-only — it imports the database. The constants and types live in
 * lib/labels.ts so the editor component can use them without dragging
 * postgres.js into the browser bundle.
 */

export { CITY_LABELS, LEVEL_LABELS, type Preferences } from "./labels";

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
    SELECT cities, keywords, exclusions, max_level, updated_at
      FROM preferences
     WHERE user_id = ${userId}
  `;
  if (!row) return explainMissingRow();
  return row;
}

/**
 * Replace this user's preferences row wholesale.
 *
 * The UPDATE is scoped by user_id, so it can only ever touch the caller's own
 * filter — under the old `WHERE id = 1` that was true by there being only one
 * row, which is a property of the schema rather than of this statement.
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
       SET cities     = ${next.cities}::text[],
           keywords   = ${next.keywords}::text[],
           exclusions = ${next.exclusions}::text[],
           max_level  = ${next.max_level},
           updated_at = now()
     WHERE user_id = ${userId}
    RETURNING cities, keywords, exclusions, max_level, updated_at
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
