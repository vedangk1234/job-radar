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
 * IT COLLIDED WITH THE SEED, AND PHASE 3 SLICE 1b IS WHERE THAT ENDED. Until
 * then a new account was seeded with DEFAULT_PREFERENCES.keywords, which is 43 —
 * so the very first save every new account attempted was refused by this cap,
 * and `onboarding_completed` is flipped by exactly that save. A brand-new user
 * was handed a filter they had not chosen and then told to remove eighteen of
 * its terms before they could choose one. NEW_ACCOUNT_SEED below is the fix:
 * a new row starts with NO keywords, which is 0 of 25 and cannot be over.
 *
 * WHAT THE COLLISION STILL MEANS, for the accounts seeded before that: they load
 * over the limit and cannot save until trimmed. The editor says so in those words
 * and refuses the save; it does NOT silently drop the extra eighteen, because a
 * filter quietly rewritten is the failure this project spends its guards on.
 * Nothing about an existing filter changes until somebody presses Save: the
 * digest goes on reading all 43 from the database in the meantime.
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
 * THE INSTALL'S OWN DEFAULT FILTER — the TypeScript mirror of notify/match.py's
 * DEFAULT_CITIES / KEYWORDS / EXCLUSIONS / MAX_LEVEL.
 *
 * IT IS NO LONGER WHAT A NEW ACCOUNT IS SEEDED WITH, and re-reading that
 * sentence is the point of this paragraph. From Phase 2a until Slice 1b it was
 * both things at once — the parity mirror AND the seed — and the second reading
 * is what shipped the operator's 43 keywords to every stranger who signed up.
 * NEW_ACCOUNT_SEED below is now the seed and the only thing lib/link-user.ts
 * reads. This constant keeps its meaning for the rows already carrying it: it is
 * the filter this install has always started from, mirrored here so the two
 * languages can be compared.
 *
 * IT IS A SEED, NOT AN OVERRIDE, exactly as the Python constants are. Nothing
 * reads this after a row exists: the database is the runtime source of truth
 * and the very next digest obeys whatever the preferences screen last saved.
 * The duplication with notify/match.py is real and is the same duplication
 * lib/match.ts already carries; `npm run match-parity` is what keeps the matcher
 * halves honest, and these four values are asserted against the Python in
 * tests/test_default_preferences.py so a drift here is a red test rather than a
 * filter nobody chose.
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
 * WHAT A BRAND-NEW ACCOUNT'S preferences ROW IS ACTUALLY CREATED WITH.
 * lib/link-user.ts branch 3 is the only reader, and this is the only thing it
 * reads.
 *
 * THE ONE DIFFERENCE FROM DEFAULT_PREFERENCES IS keywords, AND IT IS THE WHOLE
 * SLICE. A new account used to be handed all 43 of the operator's, which is
 * wrong twice over and the second way is worse than the first:
 *
 *   1. They are somebody else's answer. A person signs up to be told about the
 *      jobs THEY want, and the screen that asks them is pre-filled with a filter
 *      tuned for one particular resume — so the honest starting point is nothing
 *      at all, and every keyword on the row afterwards is one they chose.
 *   2. 43 is over KEYWORD_CAP, and the save that trips the cap is the same save
 *      that sets onboarding_completed. So the seed refused the only act that can
 *      finish onboarding: a new account could not get past the setup screen
 *      without first deleting eighteen terms it had never typed. Empty is 0 of
 *      25, so that catch-22 cannot exist — an account starting here has room for
 *      the whole cap.
 *
 * SPREAD, NOT A HAND-COPIED LITERAL. cities, exclusions and max_level are
 * deliberately unchanged, and re-typing them here is how the copy that nothing
 * compares drifts from the copy tests/test_default_preferences.py checks against
 * the Python. The spread says exactly what is true: a new account starts from
 * this install's defaults EXCEPT the one list it has to supply itself.
 *
 * WHY THE OTHER THREE ARE NOT ALSO EMPTIED. cities is refused empty by
 * PUT /api/preferences (an empty city list matches no job at all) and three
 * cities are the entire scope of the product today, so blanking it would add a
 * second thing a new account must get right for no gain. exclusions is a
 * NARROWING list — empty means nothing is excluded by title — so blanking it
 * would widen a new person's first digest to the manager, sales and recruiter
 * titles max_level cannot catch, which is the opposite of a safe default. Both
 * are editable on the same screen the moment somebody disagrees.
 */
export const NEW_ACCOUNT_SEED = {
  ...DEFAULT_PREFERENCES,
  keywords: [] as readonly string[],
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
