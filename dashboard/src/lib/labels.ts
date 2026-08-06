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
  onboarding_completed: boolean;
};

/**
 * The most keywords one account may save. Enforced in the browser AND in
 * PUT /api/preferences, because a cap that only exists in the editor is not a
 * cap — it is a suggestion to anyone holding curl.
 *
 * WHY THERE IS A CAP AT ALL. keywords is an OR-list matched against every open
 * job title, so each entry only ever widens the digest. Twenty-five is around
 * the point where a filter stops describing a person and starts describing
 * "engineering", and a resume-driven flow makes that easy to blow past without
 * noticing — extraction can offer far more than twenty-five plausible skills.
 * The cap is what makes choosing among them the point of the screen.
 *
 * IT COLLIDES WITH DEFAULT_PREFERENCES.keywords, WHICH IS 43, AND THAT IS
 * DELIBERATE RATHER THAN OVERLOOKED. Every account seeded before this cap
 * existed — including the operator's — loads over the limit and cannot save
 * until it is trimmed. The editor says so in those words and refuses the save;
 * it does NOT silently drop the extra eighteen, because a filter quietly
 * rewritten is the failure this project spends its guards on. Nothing about an
 * existing filter changes until somebody presses Save: the digest goes on
 * reading all 43 from the database in the meantime.
 */
export const KEYWORD_CAP = 25;

/**
 * The stored resume, as the profile screen shows it.
 *
 * Here rather than in lib/resume.ts for the reason this whole file exists: the
 * upload panel is a client component, and lib/resume.ts imports postgres.js. A
 * type would be safe to import from anywhere — types are erased — but keeping
 * the shape beside the other client-facing types means the component never
 * reaches into a server module at all, by habit rather than by luck.
 */
export type StoredResume = {
  filename: string;
  content_type: string;
  size_bytes: number;
  uploaded_at: string;
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
 * The filter a brand-new account starts with — the TypeScript mirror of
 * notify/match.py's DEFAULT_CITIES / KEYWORDS / EXCLUSIONS / MAX_LEVEL.
 *
 * Phase 2a is what makes this necessary. Until now the only preferences row was
 * created by db/schema.sql's seed, in SQL, once; from 2a a stranger can sign up
 * and lib/link-user.ts has to give them a row in the same transaction that
 * creates their account — because loadPreferences() throws when there is none,
 * and Home, /jobs and /preferences would all 500 on their first page load.
 *
 * IT IS A SEED, NOT AN OVERRIDE, exactly as the Python constants are. Nothing
 * reads this after the row exists: the database is the runtime source of truth
 * and the very next digest obeys whatever the preferences screen last saved.
 * The duplication with notify/match.py is real and is the same duplication
 * lib/match.ts already carries; `npm run match-parity` is what keeps the matcher
 * halves honest, and these four values are asserted against the Python in
 * tests/test_default_preferences.py so a drift here is a red test rather than a
 * new user silently starting with a filter nobody chose.
 */
export const DEFAULT_PREFERENCES = {
  cities: ["blr", "pune", "hyd"],
  keywords: [
    "software engineer", "software developer", "software development engineer", "sde",
    "associate software engineer", "member of technical staff", "mts",
    "application developer", "programmer", "forward deployed engineer",
    "backend", "back end", "node", "nodejs", "express", "fastapi", "api", "rest",
    "microservices", "distributed", "full stack", "fullstack", "frontend", "front end",
    "react", "react native", "nextjs", "next.js", "python", "javascript", "typescript",
    "platform", "infrastructure", "infra", "devops", "cloud engineer",
    "machine learning", "ml engineer", "ai engineer", "mlops", "deep learning",
    "computer vision", "llm",
  ],
  exclusions: [
    "intern", "senior", "staff", "principal", "lead", "manager", "director", "head",
    "architect", "sales", "recruiter", "support", "qa", "analyst",
  ],
  max_level: 2,
} as const;

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
