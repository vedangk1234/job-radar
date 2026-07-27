import { sql } from "./db";
import { CITY_LABELS, type Preferences } from "./labels";

/**
 * The single preferences row — the filter that decides what reaches an inbox.
 *
 * db/schema.sql pins this to exactly one row (PRIMARY KEY DEFAULT 1 CHECK
 * (id = 1)), which is why nothing here takes an id. Editing it is the whole
 * point of the dashboard: the crawler reads it on every digest, so a change
 * saved here is live on the next run with no redeploy and no commit.
 *
 * Server-only — it imports the database. The constants and types live in
 * lib/labels.ts so the editor component can use them without dragging
 * postgres.js into the browser bundle.
 */

export { CITY_LABELS, LEVEL_LABELS, type Preferences } from "./labels";

export async function loadPreferences(): Promise<Preferences> {
  const [row] = await sql<Preferences[]>`
    SELECT cities, keywords, exclusions, max_level, updated_at
      FROM preferences
     WHERE id = 1
  `;
  if (!row) {
    throw new Error(
      "the preferences row is missing. Re-run `python scripts/init_db.py` to seed it.",
    );
  }
  return row;
}

export async function savePreferences(next: {
  cities: string[];
  keywords: string[];
  exclusions: string[];
  max_level: number;
}): Promise<Preferences> {
  const [row] = await sql<Preferences[]>`
    UPDATE preferences
       SET cities     = ${next.cities}::text[],
           keywords   = ${next.keywords}::text[],
           exclusions = ${next.exclusions}::text[],
           max_level  = ${next.max_level},
           updated_at = now()
     WHERE id = 1
    RETURNING cities, keywords, exclusions, max_level, updated_at
  `;
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
