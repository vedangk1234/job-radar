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

/**
 * The preferences ROW, as the editor and the pages see it.
 *
 * Deliberately a superset of lib/match.Preferences, which is the subset the RULES
 * read. The extra fields here are the ones a screen needs and a verdict must not
 * have: `updated_at` ("last saved"), `onboarding_completed` (the gate), and
 * `years_of_experience_held` — how much experience the person HAS, which is
 * informational and never filters. Structural typing means this is assignable to
 * the matcher's type wherever a rule is evaluated, without the matcher's type ever
 * gaining a field it has no business reading.
 */
export type Preferences = {
  cities: string[];
  selected_cities: string[];
  all_india: boolean;
  keywords: string[];
  exclusions: string[];
  max_level: number;
  exp_range_low: number | null;
  exp_range_high: number | null;
  years_of_experience_held: number | null;
  show_no_experience_jobs: boolean;
  work_modes: string[];
  salary_bands: string[];
  updated_at: string;
  onboarding_completed: boolean;
};

/**
 * THE THREE WORK ARRANGEMENTS A PERSON MAY TICK — the picker's vocabulary, and
 * the mirror of notify.match.WORK_MODE_LABELS.
 *
 * Every label is offered, unlike CITY_LABEL_GROUPS which withholds two of the 26.
 * The reason the city list has exclusions does not apply here: `india-unclear`
 * and `non-india` are statements about our confidence and about a country, and
 * neither has an analogue in a three-value enum where all three are things a
 * person can coherently want.
 *
 * The blurbs are not decoration. `onsite` is what a NULL jobs.work_mode is read
 * as (lib/match.DEFAULT_WORK_MODE), which means unticking it hides the large
 * majority of the board — every posting whose description nobody has read. That
 * is a surprising amount of consequence for one checkbox, so the screen says so.
 */
export const WORK_MODE_OPTIONS: ReadonlyArray<{
  value: string;
  label: string;
  hint: string;
}> = [
  {
    value: "onsite",
    label: "On-site",
    hint: "Also every job whose description we have not read — which is most of them. Unticking this hides almost the whole board.",
  },
  { value: "hybrid", label: "Hybrid", hint: "Some days in an office, some at home." },
  {
    value: "remote",
    label: "Remote",
    hint: "Fully remote. A remote job matches wherever it is in India, even outside the cities you picked.",
  },
];

/** Every value a person may put in work_modes. */
export const WORK_MODE_VALUES: readonly string[] = WORK_MODE_OPTIONS.map((o) => o.value);

/**
 * THE SIX PAY BANDS A PERSON MAY TICK — the picker's vocabulary, in LPA.
 *
 * THE `value` STRINGS ARE NOT NEGOTIABLE AND WERE NOT CHOSEN HERE. They were
 * written by scripts/backfill_onboarding_prefs.py when Phase 3 Step 0 landed, and
 * that script has already run: the live preferences row holds exactly these six,
 * and the script's own verification asserts the set still matches. Respelling them
 * as "<10L" / "50L+" would leave a live row full of labels this vocabulary does
 * not admit — and because the gate is a membership test the symptom would be
 * silent, hiding every PRICED job on a board where nothing is priced yet, so it
 * would ship green and bite on the first posting that stated pay.
 *
 * The `label` is where the L goes. Mirrored in notify/match.SALARY_BAND_LABELS,
 * whose SALARY_BAND_BOUNDS carry the arithmetic; this file has no database import
 * and no opinion about overlap, exactly as it has none about city adjacency.
 *
 * THE HINT ON THE PANEL SAYS THE QUIET PART, and it has to. Unlike every other
 * filter on the screen, ticking a band here changes almost nothing: ~0% of
 * postings state pay, an unpriced job is shown regardless, and a person who ticks
 * `<10` expecting a short list will get the entire board. Better read than
 * discovered — the same courtesy the on-site hint above and the "99% state no
 * minimum" hint one panel up already pay.
 */
export const SALARY_BAND_OPTIONS: ReadonlyArray<{
  value: string;
  label: string;
}> = [
  { value: "<10", label: "Under ₹10L" },
  { value: "10-15", label: "₹10L – ₹15L" },
  { value: "15-25", label: "₹15L – ₹25L" },
  { value: "25-40", label: "₹25L – ₹40L" },
  { value: "40-50", label: "₹40L – ₹50L" },
  { value: "50+", label: "₹50L and above" },
];

/** Every value a person may put in salary_bands. */
export const SALARY_BAND_VALUES: readonly string[] = SALARY_BAND_OPTIONS.map((o) => o.value);

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
 * WHERE AN ACCOUNT THAT HAS NOT CHOSEN A FILTER YET IS SENT.
 *
 * The literal used to live in lib/guard.ts, which is still the only thing that
 * REDIRECTS to it — but it is here now because a second reader appeared and that
 * reader is a client component. components/Nav hides the tabs on this route, and
 * a client component importing a VALUE from lib/guard would drag lib/preferences,
 * and therefore postgres.js, into the browser bundle: the exact failure this file
 * exists to prevent, stated at the top of it.
 *
 * lib/guard re-exports it, so every existing caller is unmoved and there is still
 * one spelling of the path in the app. That is the point rather than tidiness: the
 * gate's destination and the route the chrome disappears on have to be the same
 * string, or a rename leaves a person mid-setup looking at links that bounce them
 * straight back here. `npm run onboarding-check` asserts the two are equal.
 */
export const ONBOARDING_PATH = "/onboarding";

/**
 * HOW MANY COMPANIES A FREE ACCOUNT MAY WATCH — the pricing card's promise, made
 * enforceable.
 *
 * The number is not chosen here. The landing page has said "Watch up to 5
 * companies" under the Free tier since the pricing section shipped, and until the
 * watch-list picker existed there was nothing anywhere that could enforce it —
 * `user_companies` had no writer in the web app at all, so the limit was a
 * sentence on a marketing page rather than a rule. This constant is that sentence,
 * moved to where the write happens; app/signin/page.tsx and this line have to say
 * the same number, and `tests/test_watchlist_write.py` asserts that they do.
 *
 * ENFORCED IN lib/watchlist.watchCompany AND NOT ONLY IN THE PICKER, for the
 * reason KEYWORD_CAP is enforced in the route rather than only in the editor: a
 * cap that lives in the browser is a suggestion to anyone holding curl. The
 * server-side half is the real one; the picker greys out the Add buttons so a
 * person is told before they press rather than after.
 *
 * IT REFUSES ADDITIONS AND NEVER REMOVES ANYTHING, and that asymmetry is
 * deliberate rather than an omission. The operator's account watches all 50
 * catalog companies (scripts/backfill_user_companies.py put them there in Phase
 * 1c, long before any limit existed), and a cap that trimmed a watch list to fit
 * would silence a digest that has been running for months — the cry-wolf failure
 * inverted. An over-cap account keeps every row it has, sees them all on Home,
 * and simply cannot add another.
 */
export const FREE_WATCHLIST_CAP = 5;

/**
 * How many companies THIS account may watch, from `public.users.plan`.
 *
 * `null` means no limit. The column is `TEXT NOT NULL DEFAULT 'free'` with no
 * CHECK (db/schema.sql, matching companies.category), so every account on this
 * install is `free` today and the paid tiers are a pricing page rather than a
 * product — see the landing copy, which says so out loud.
 *
 * UNKNOWN PLANS ARE UNLIMITED RATHER THAN CAPPED, which is the one judgement call
 * in this function. The alternative — anything not recognised gets the free cap —
 * fails in the direction that takes something away from a person who paid, on the
 * strength of a string this file has never heard of. A cap wrongly not applied
 * costs some rows in a table; a cap wrongly applied is somebody's paid watch list
 * refusing to grow with a message about a free tier they are not on.
 */
export function watchlistCapForPlan(plan: string | null | undefined): number | null {
  return (plan ?? "free").trim().toLowerCase() === "free" ? FREE_WATCHLIST_CAP : null;
}

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
 *
 * This is a MIRROR of fetchers/normalize.CITY_LABELS, in that order, and
 * tests/test_city_vocabulary.py parses this file and asserts the two are
 * identical. The list is duplicated rather than shared because the two runtimes
 * share no code at all (see CLAUDE.md, "Two runtimes, one database") — so the
 * duplication is a fact of the architecture and the test is what keeps it from
 * becoming a lie.
 *
 * `other` was here until the 25-city rewrite and is deliberately gone: it used
 * to mean three different things at once (an Indian city we had not listed, a
 * confidently foreign posting, and a string no rule recognised), and the first
 * two are now `other-india` and `non-india`. Nothing maps to `other` any more,
 * so leaving it in the list would offer a filter that matches zero rows —
 * exactly the silent-empty failure this constant exists to prevent.
 */
export const CITY_LABELS = [
  // the three legacy spellings — see fetchers/normalize for why they are not
  // `bengaluru` / `hyderabad`
  "blr",
  "hyd",
  "pune",
  // Mumbai region
  "navi-mumbai",
  "mumbai-suburban",
  "thane",
  "mumbai",
  // NCR
  "new-delhi",
  "gurugram",
  "noida",
  // the rest
  "chennai",
  "kolkata",
  "ahmedabad",
  "indore",
  "nagpur",
  "nashik",
  "chhatrapati-sambhajinagar",
  "coimbatore",
  "madurai",
  "hubballi",
  "mangaluru",
  "mysuru",
  "belagavi",
  // the buckets
  "other-india",
  "non-india",
  "india-unclear",
] as const;

export type CityLabel = (typeof CITY_LABELS)[number];

/**
 * THE LABELS AN EXPLICIT CITY SELECTION MAY CONTAIN — 24 of the 26.
 *
 * `india-unclear` and `non-india` are deliberately absent, and this constant is
 * the FIRST of the three layers that keep them out (the editor renders from these
 * groups, PUT /api/preferences validates against this list, and
 * notify.match.CITY_SELECTION_NEVER_MATCHES is the property rather than the
 * promise). Read that constant for the argument; the short version is that
 * india-unclear is a statement about our confidence rather than about a location,
 * so it belongs to the all-India switch, and non-india is the one label all-India
 * excludes by definition — an explicit selection able to match it would mean the
 * narrow filter admitting rows the wide one refuses.
 *
 * `other-india` IS here. It is a confident statement that the posting is in India
 * at a named place outside the 23, so ticking it means "anywhere else in India",
 * which is a coherent thing to want.
 *
 * NOT A SECOND VOCABULARY. CITY_LABELS above is still the full 26 and is still
 * what tests/test_city_vocabulary.py mirrors against fetchers/normalize — the
 * labeller can produce all of them and jobs.city holds all of them. This is the
 * subset a PERSON may choose, which is a different question, and keeping the two
 * constants separate is what stops "we stopped offering a label" from silently
 * becoming "we stopped labelling with it".
 *
 * `umbrella` is the tick-one-get-all control. It is PRESENTATION AND A SAVE-TIME
 * EXPANSION, never a stored value: ticking it selects its members and
 * selected_cities stores the members, so notify/match.py never learns that
 * "Mumbai region" is a thing. See PUT /api/preferences for why the expansion
 * happens at the route as well as in the browser.
 */
export const CITY_LABEL_GROUPS: ReadonlyArray<{
  heading: string;
  labels: readonly string[];
  umbrella?: string;
}> = [
  { heading: "Target cities", labels: ["blr", "hyd", "pune"] },
  {
    heading: "Mumbai region",
    umbrella: "All Mumbai areas",
    labels: ["mumbai", "navi-mumbai", "mumbai-suburban", "thane"],
  },
  {
    heading: "Delhi NCR",
    umbrella: "All of Delhi/NCR",
    labels: ["new-delhi", "gurugram", "noida"],
  },
  {
    heading: "Other listed cities",
    labels: [
      "chennai", "kolkata", "ahmedabad", "indore", "nagpur", "nashik",
      "chhatrapati-sambhajinagar", "coimbatore", "madurai", "hubballi",
      "mangaluru", "mysuru", "belagavi",
    ],
  },
  { heading: "Elsewhere in India", labels: ["other-india"] },
];

/**
 * HOW A CITY LABEL IS SPELLED ON A SCREEN — presentation, and nothing else.
 *
 * `blr` is the vocabulary: it is what `jobs.city` holds, what `selected_cities`
 * stores, and what both matchers compare. It is also not what anybody calls the
 * place, and a person choosing where they would take a job should not have to
 * learn this project's slugs to do it. So the picker reads "Bengaluru" and keeps
 * the slug on the control's `title` and `aria-label`, which is the same direction
 * LEVEL_LABELS took for `max_level`: the number decides, the words are for the
 * reader.
 *
 * IT IS DELIBERATELY NOT A SECOND VOCABULARY, and the lookup is one-way. Nothing
 * parses these back into labels, nothing sends them, and a name missing from this
 * map falls back to the slug rather than throwing — a city added to
 * CITY_LABEL_GROUPS and forgotten here renders as `hubballi` and still works,
 * which is the failure mode worth having.
 */
export const CITY_DISPLAY_NAMES: Readonly<Record<string, string>> = {
  blr: "Bengaluru",
  hyd: "Hyderabad",
  pune: "Pune",
  mumbai: "Mumbai",
  "navi-mumbai": "Navi Mumbai",
  "mumbai-suburban": "Mumbai Suburban",
  thane: "Thane",
  "new-delhi": "New Delhi",
  gurugram: "Gurugram",
  noida: "Noida",
  chennai: "Chennai",
  kolkata: "Kolkata",
  ahmedabad: "Ahmedabad",
  indore: "Indore",
  nagpur: "Nagpur",
  nashik: "Nashik",
  "chhatrapati-sambhajinagar": "Chhatrapati Sambhajinagar",
  coimbatore: "Coimbatore",
  madurai: "Madurai",
  hubballi: "Hubballi",
  mangaluru: "Mangaluru",
  mysuru: "Mysuru",
  belagavi: "Belagavi",
  "other-india": "Elsewhere in India",
};

/** The name to print for a label, falling back to the label itself. */
export function cityDisplayName(label: string): string {
  return CITY_DISPLAY_NAMES[label] ?? label;
}

/**
 * HOW A CITY LABEL IS SPELLED ON A CARD CHIP — uppercase, and the same one
 * vocabulary.
 *
 * `blr` becomes `BLR` and `pune` becomes `PUNE`. It is deliberately NOT an
 * airport code: cities Slice A tested and rejected all fourteen of them for the
 * new cities because they carried zero rows, and a chip that read `PNQ` would be
 * a second city vocabulary invented for one line of one screen — the exact drift
 * CITY_DISPLAY_NAMES is documented as not being. The friendly name still travels,
 * on the chip's `title`, which is where a reader who does not know `blr` looks.
 *
 * Case only, so it cannot fail: a label missing from every map still renders as
 * itself, shouted.
 */
export function cityChipLabel(label: string): string {
  return label.toUpperCase();
}

/**
 * HOW A KEYWORD IS SPELLED ANYWHERE IT IS SHOWN — `llm` renders as `LLM`.
 *
 * A DISPLAY TRANSFORM AND NOTHING ELSE, and the direction matters. The stored
 * value stays lowercase, because notify/match.py compares against a normalised,
 * lowercased title: a keyword stored as `LLM` would match nothing at all. So
 * KeywordChips still lowercases on the way IN (see its `add`), lib/preferences
 * .cleanList still lowercases on the way to the database, and this is applied on
 * the way OUT, at render.
 *
 * IN JAVASCRIPT RATHER THAN IN CSS `text-transform`, which would be the purer
 * display-only mechanism and is the wrong tool here: `npm run ui-check` reads
 * MARKUP and has no browser, so a CSS-only rule would be unassertable — and an
 * ALL-CAPS rule nothing can check is the shape of a rule that quietly stops
 * holding. It lives in this file, with no database import, so a client component
 * can call it without dragging postgres.js into the browser bundle.
 */
export function displayKeyword(keyword: string): string {
  return keyword.toUpperCase();
}

/** Every label a person may put in selected_cities, flattened from the groups. */
export const SELECTABLE_CITY_LABELS: readonly string[] = CITY_LABEL_GROUPS.flatMap(
  (group) => group.labels,
);

/**
 * Tick the umbrella, get these. Derived from the groups rather than restated, so
 * a city added to a group above cannot be forgotten here.
 */
export const CITY_UMBRELLAS: ReadonlyArray<{ label: string; members: readonly string[] }> =
  CITY_LABEL_GROUPS.filter((g) => g.umbrella).map((g) => ({
    label: g.umbrella as string,
    members: g.labels,
  }));

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
  // The city gate's new inputs, mirroring notify/match.DEFAULT_SELECTED_CITIES and
  // DEFAULT_ALL_INDIA. BOTH ARE BLANK, and the blank is what makes this constant
  // still describe the install's filter: an empty selected_cities defers to
  // `cities` above (notify.match.Preferences.effective_cities), so the fallback
  // filter a database with no preferences row runs on is the same three cities it
  // has always been. all_india FALSE is the closed default — TRUE reached by
  // omission would be the widest possible filter, chosen by nobody.
  selected_cities: [],
  all_india: false,
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
  // The experience rule, mirroring notify/match.DEFAULT_EXP_RANGE_LOW / _HIGH /
  // DEFAULT_SHOW_NO_EXPERIENCE_JOBS. (0, 2) reproduces the fixed `<= 2` ceiling
  // this install ran on until Slice 2 — min_experience is never negative, so
  // {0,1,2} and "at most 2" select the same rows — and TRUE is the "unknown means
  // keep" bias that ceiling was paired with.
  //
  // NOT the same thing as the COLUMN defaults, which are NULL / NULL / FALSE and
  // mean "nobody has answered". These three are the install's answer, which is
  // what a row with no owner falls back to and what tests/test_default_
  // preferences.py compares against the Python.
  exp_range_low: 0,
  exp_range_high: 2,
  show_no_experience_jobs: true,
  // All three, mirroring notify/match.DEFAULT_WORK_MODES and what
  // scripts/backfill_onboarding_prefs.py wrote onto the operator's row: nothing
  // is filtered on arrangement. NOT the same as the COLUMN default, which is
  // '{}' and means "nobody has ticked anything" — an empty selection matches no
  // job at all. The distinction is the same one the experience range draws.
  work_modes: ["onsite", "hybrid", "remote"],
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
 *     SLICE 2 WIRED THREE OF THEM UP AND THIS PARAGRAPH STILL HOLDS, which is
 *     worth one sentence because it is the first time a column named here became
 *     load-bearing. A new account's experience range arrives NULL, and NULL now
 *     means "matches nothing" rather than "unread" — so an account in that state
 *     must not be able to reach a digest or a match count, and it cannot: the
 *     gate holds it on /onboarding, `is_active` is FALSE, and
 *     PUT /api/preferences refuses a save whose range is incomplete. Blank is
 *     still the honest seed; what changed is that finishing onboarding is now the
 *     only way out of it.
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
