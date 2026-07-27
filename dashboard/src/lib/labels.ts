/**
 * Shared constants and types with NO database import.
 *
 * That constraint is the whole reason this file is separate from
 * lib/preferences.ts. A client component that imports a value (not just a type)
 * from a module drags that module's entire import graph into the browser
 * bundle — and lib/preferences.ts imports postgres.js, which needs Node's `net`
 * and fails the build. Types are erased at compile time and are safe to import
 * from anywhere; values are not.
 */

export type Preferences = {
  cities: string[];
  keywords: string[];
  exclusions: string[];
  max_level: number;
  updated_at: string;
};

/**
 * The only city labels fetchers/normalize.city_label can produce. A city in
 * preferences outside this set matches nothing, silently, forever — so a typo
 * is rejected at the edit rather than discovered three empty digests later.
 */
export const CITY_LABELS = ["blr", "pune", "hyd", "india-unclear", "other"] as const;

export type CityLabel = (typeof CITY_LABELS)[number];

/**
 * The hand-tags companies.category is allowed to hold, per db/schema.sql.
 *
 * `staffing` is in the list even though it is rarer than the other four: the
 * column already permits it, and a dropdown that cannot represent a value the
 * database holds would silently rewrite that row the first time anyone touched
 * the tag next to it.
 */
export const CATEGORIES = ["product", "gcc", "services", "staffing", "unknown"] as const;

export type Category = (typeof CATEGORIES)[number];

/**
 * notify/match.detect_level's scale, spelled out. Kept as labels rather than
 * bare integers because "max_level = 2" means nothing on a screen.
 */
export const LEVEL_LABELS: Record<number, string> = {
  0: "0 — intern / trainee",
  1: "1 — junior / associate / entry",
  2: "2 — mid (no level stated)",
  3: "3 — senior",
  4: "4 — staff / principal / lead",
  5: "5 — manager / director / head",
};
