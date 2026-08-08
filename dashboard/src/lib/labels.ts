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
 * IT IS BLANK ON EVERY FIELD, and that is the whole of it. Slice 1b emptied
 * `keywords` alone and left cities, exclusions and max_level as this install's
 * defaults; this slice finishes the argument Slice 1b started, because the
 * argument was never about keywords in particular.
 *
 * WHY EMPTYING keywords WAS NEVER THE POINT. Slice 1b had two reasons and only
 * the second was specific to that list:
 *
 *   1. They are somebody else's answer. A person signs up to be told about the
 *      jobs THEY want, and the screen that asks them was pre-filled with a
 *      filter tuned for one particular resume.
 *   2. 43 was over KEYWORD_CAP, and the save that trips the cap is the same save
 *      that sets onboarding_completed — so the seed refused the only act that
 *      finishes onboarding.
 *
 * Reason 2 is what made keywords urgent; reason 1 is true of every field on the
 * row, and it is the one that decides what this constant should be. A seeded
 * `cities` of blr/pune/hyd is the operator's answer to "where do you want to
 * work". A seeded `exclusions` of fourteen terms is the operator's answer to
 * "what should never reach you" — it silently drops every architect and analyst
 * posting for somebody who may be looking for exactly those. `max_level = 2` is
 * the operator's answer to "how senior". None of those were chosen by the person
 * holding the account, and a default nobody chose is indistinguishable, on the
 * screen and in the digest, from a decision they made.
 *
 * A HAND-WRITTEN LITERAL, NOT A SPREAD, WHICH REVERSES SLICE 1b's DECISION ON
 * PURPOSE. The spread existed to stop three copied values drifting from the
 * Python — a real hazard when the seed genuinely was the defaults. Empty cannot
 * drift: there is no value here to disagree with notify/match.py about. Worse,
 * a spread now would be actively dangerous in the one direction that matters —
 * a field added to DEFAULT_PREFERENCES tomorrow would arrive on new accounts as
 * the operator's answer, silently, which is precisely the bug this slice exists
 * to end. Spelling the blanks out means a new field is absent from a new
 * account's row until somebody deliberately adds it here.
 *
 * WHAT IS NOT HERE, AND WHY THAT IS THE BLANK RATHER THAN AN OMISSION:
 *
 *   - `max_level`. The column is `INT NOT NULL DEFAULT 2` (db/schema.sql), so
 *     NULL is not a legal value for it and "unset" cannot be spelled that way.
 *     The schema default IS the unset state, and branch 3 reaches it by leaving
 *     the column out of its INSERT rather than by binding anything from here.
 *     Naming it in this object would mean binding a number, and the only honest
 *     number is the one the schema already supplies.
 *   - The nine columns Phase 3 Step 0 added to preferences — the city selection,
 *     the all-India switch, the work-mode and salary-band lists, the
 *     no-experience switch, the three experience ints, and the onboarding flag.
 *     Every one of them already declares a default that IS blank (`'{}'`,
 *     `FALSE`, or NULL for the nullable ints), so an INSERT that does not name
 *     them produces exactly the row this constant describes.
 *
 *     They are described here rather than listed by name on purpose, and this
 *     file is one of the reasons: tests/test_phase3_columns.py sweeps every
 *     module that decides anything — this one and lib/link-user.ts included — and
 *     fails if it so much as mentions a column Step 0 created and no feature step
 *     has yet wired up. A name in a comment is where the next edit starts.
 *
 * THE ROW THIS PRODUCES CANNOT BE SAVED AS-IS, AND THAT IS CORRECT.
 * PUT /api/preferences refuses an empty `cities` and an empty `keywords`, because
 * either matches no job at all. A brand-new account is therefore incomplete until
 * onboarding, which is what onboarding is for: `onboarding_completed` is FALSE, so
 * lib/guard.requirePage holds them on /onboarding until they answer. Nothing
 * about signup depends on the row being savable — branch 3 writes it directly and
 * never goes through that route — so a blank seed cannot fail a sign-in.
 * `exclusions` stays legal empty either way; it is a narrowing list, and empty
 * honestly means "this person has not said what to drop yet".
 */
export const NEW_ACCOUNT_SEED = {
  cities: [] as readonly string[],
  keywords: [] as readonly string[],
  exclusions: [] as readonly string[],
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
