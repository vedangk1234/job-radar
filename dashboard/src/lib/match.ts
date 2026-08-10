/**
 * notify/match.py, ported to TypeScript. Pure functions, no I/O, no database.
 *
 * WHY THIS FILE EXISTS AT ALL
 * ---------------------------
 * The dashboard's "For you" count has to be the SAME number the digest email
 * would contain. Not similar — the same. If Home says a job fits and the email
 * never arrives, the dashboard is lying about the one thing it is for.
 *
 * The obvious way to guarantee that is to call the Python. The dashboard cannot:
 * it deploys to Vercel with no Python runtime. The second-most obvious way is to
 * re-express the rules in SQL, which fails on `detect_level` — masking the
 * experience numbers out of "Ops Engineer 2 to 5 Years" before reading "2" as a
 * seniority level is procedural, and a SQL translation of it would be a second
 * implementation with none of the tests.
 *
 * So this is a port, and it is treated like one: `scripts/match-parity.ts` runs
 * this file and notify/match.py over every open row in the live database and
 * fails on the first disagreement. That is the same arrangement lib/ats.ts has
 * with fetchers/resolve.py — duplicated logic drifts, so the drift is made into
 * a failing check rather than a wrong number nobody notices.
 *
 * Read notify/match.py for the reasoning behind each rule; it is the original
 * and it carries the argument. This file carries only the translation notes.
 *
 * ONE DELIBERATE NON-DIFFERENCE
 * -----------------------------
 * `desc_checked` is not consulted here, exactly as it is not consulted in
 * Python. An un-enriched row arrives with is_pm_role = FALSE and
 * min_experience = NULL, and neither of those is disqualifying on its own. So
 * "description never read" and "description read, nothing disqualifying found"
 * produce the same verdict, and the dashboard shows the same set either way.
 *
 * WHAT CHANGED IN SLICE 2: a NULL min_experience is no longer KEEP
 * unconditionally. It defers to preferences.show_no_experience_jobs, so the two
 * states above still agree with each other — they just now agree on whatever the
 * account answered rather than always on "keep". `desc_checked` is still not
 * consulted, which is the part this paragraph is about.
 *
 * What the dashboard does extra is
 * SAY SO: `descriptionHint` turns desc_checked = false into a visible
 * "desc not checked" note, so a match that rests on an unread description is
 * legible rather than implied. It changes the label, never the answer.
 *
 * That note used to be the normal state of a fresh job and is now close to
 * extinct. Enrichment runs in the CRAWL (crawler/enrich.py), not only when a
 * digest goes out, so a matching job's description is read within one crawl
 * cycle of it appearing rather than whenever an email next happens. The hint
 * stays because the window still exists — it is just minutes wide now, and a
 * match resting on an unread description is exactly as worth flagging as it
 * always was.
 */

/**
 * The filter, as the rules read it — SEVEN fields since Phase 3 Slice 2.
 *
 * `years_of_experience_held` is deliberately NOT here, matching
 * notify.match.Preferences. It is informational (how much experience the person
 * HAS) and must never filter, so it is kept off the type the rules read: a field
 * that does not exist cannot be consulted by a rule added later. lib/labels
 * `Preferences` — the shape of the ROW, which the editor needs — does carry it,
 * and is structurally assignable to this.
 *
 * The two range ends are nullable because NULL is meaningful on those columns:
 * "this account has not configured a range". See experienceExcluded.
 */
export type Preferences = {
  cities: readonly string[];
  selected_cities: readonly string[];
  all_india: boolean;
  keywords: readonly string[];
  exclusions: readonly string[];
  max_level: number;
  exp_range_low: number | null;
  exp_range_high: number | null;
  show_no_experience_jobs: boolean;
  work_modes: readonly string[];
};

/**
 * Anything with the columns the rules read. Both job shapes in the app satisfy it.
 *
 * `work_mode` joined the list in cities Slice B and is OPTIONAL for the reason the
 * rest are: a caller that has not selected it hands over `undefined`, which
 * `isRemote` reads as not-remote — the same answer the column's NULL gives, which
 * is the answer for every row in the database today. lib/jobs.jobColumns does
 * select it, so the app's own rows carry the real value the moment the parser
 * fills it in.
 */
export type MatchableJob = {
  title?: string | null;
  city?: string | null;
  work_mode?: string | null;
  min_experience?: number | null;
  is_pm_role?: boolean | null;
  desc_checked?: boolean | null;
  desc_fetch_failed?: boolean | null;
};

/** notify/match.NON_INDIA_LABEL. The one label that is never India. */
export const NON_INDIA_LABEL = "non-india";

/** notify/match.CITY_SELECTION_NEVER_MATCHES — read that constant for the argument. */
export const CITY_SELECTION_NEVER_MATCHES: ReadonlySet<string> = new Set([
  "india-unclear",
  NON_INDIA_LABEL,
]);

/** notify/match.REMOTE_WORK_MODE. The one value the city relaxation cares about. */
export const REMOTE_WORK_MODE = "remote";

/** notify/match.WORK_MODE_LABELS. Mirrored in lib/labels.WORK_MODE_OPTIONS. */
export const WORK_MODE_LABELS = ["onsite", "hybrid", "remote"] as const;

/**
 * notify/match.DEFAULT_WORK_MODE — what a NULL jobs.work_mode is READ as.
 *
 * Never written back. NULL means "nobody has established this posting's
 * arrangement", which is most of the board and always will be, since a
 * description is only fetched for a job some filter already wants. Collapsing
 * that to onsite is a filtering decision made here, at the comparison; writing
 * it into the column would record a conclusion as an observation and lose the
 * read/unread distinction — jobs.notified, one table over. Read the Python.
 */
export const DEFAULT_WORK_MODE = "onsite";

/*
 * MAX_MIN_EXPERIENCE USED TO BE EXPORTED FROM HERE AND IS GONE.
 *
 * It was `2`, mirroring notify/match.MAX_MIN_EXPERIENCE, whose comment said "if
 * it ever becomes a preferences column it becomes one in both files on the same
 * day". Phase 3 Slice 2 is that day: the ceiling is now
 * preferences.exp_range_high, the rule is a range, and both files changed
 * together. Deleted rather than left at 2 for the reason the Python states — a
 * value nothing reads invites the next reader to assume something does.
 *
 * lib/jobs.ts imported it for the SQL prefilter and now builds that clause from
 * the preferences row instead. The install's own default values live in
 * lib/labels.DEFAULT_PREFERENCES, where tests/test_default_preferences.py
 * compares them against the Python constants.
 */

// --- word-boundary terms ---------------------------------------------------

/** Inside a multi-word term, what may stand between the words. */
const INNER_SEP = String.raw`[\s\-_/]+`;

/**
 * Escape for a regex literal.
 *
 * Python's `re.escape` also escapes `-`, `&`, `~`, `#` and whitespace. Those are
 * not special in JavaScript outside a character class, so escaping them or not
 * produces the same LANGUAGE — and the language is what has to match, not the
 * pattern string. Whitespace never reaches here anyway: terms are split on it.
 */
function escapeRe(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

const termCache = new Map<string, RegExp | null>();

/**
 * One preference term -> a word-boundary regex, or null if it is empty.
 *
 *   'python'    -> \bpython\b
 *   'back end'  -> \bback[\s\-_/]+end\b   ("back-end", "back_end", "back/end")
 *   '.net'      -> no left \b: the edge is punctuation, where \b asserts the
 *                  opposite of what is wanted.
 */
export function compileTerm(term: string): RegExp | null {
  const cached = termCache.get(term);
  if (cached !== undefined) return cached;

  const tokens = term.trim().toLowerCase().split(/\s+/).filter(Boolean).map(escapeRe);
  if (!tokens.length) {
    termCache.set(term, null);
    return null;
  }

  const body = tokens.join(INNER_SEP);
  const left = /^\w/.test(tokens[0]) ? String.raw`\b` : "";
  const right = /\w$/.test(tokens[tokens.length - 1]) ? String.raw`\b` : "";
  const compiled = new RegExp(`${left}${body}${right}`, "i");
  termCache.set(term, compiled);
  return compiled;
}

/** Which of `terms` appear in `title`, as whole words. Order preserved. */
export function matchedTerms(title: string | null | undefined, terms: readonly string[]): string[] {
  const text = (title ?? "").toLowerCase();
  if (!text) return [];
  return terms.filter((t) => compileTerm(t)?.test(text));
}

// --- seniority level in the title ------------------------------------------

const ROMAN_LEVELS: Record<string, number> = { ii: 2, iii: 3, iv: 4, v: 5, vi: 6 };

// Longest alternative first so "VI" reads as 6, not 5 followed by a stray 'i'.
const ROMAN_RE = /\b(vi|iv|iii|ii|v)\b/gi;

const LEVEL_TOKEN = "(?:sde|swe|sdet|mts|engineer|developer|analyst|grade)";

const DIGIT_LEVEL_RES: RegExp[] = [
  /\bl([2-9])\b/gi, //                                    L3, L4
  /\blevel[\s-]*([2-9])\b/gi, //                          Level 5, level-4
  new RegExp(String.raw`\b${LEVEL_TOKEN}\b[\s-]*([2-9])\b`, "gi"), // SDE 3
  /[\s-]([2-9])\s*$/g, //                                 "... 3" at the end
];

const NUMBER_RE = /\d+/g;

/*
 * The three shapes that mean a number is EXPERIENCE and not a level. All three
 * are anchored the way Python anchors them: the two "after" patterns use the
 * sticky flag, which is exactly `re.match(text, pos)` — match AT this offset,
 * not somewhere later — and the "before" pattern ends in `$` against the text
 * to the left of the number.
 */
const YEARS_AFTER_RE = /\s*\+?\s*(?:years?|yrs?|y\s*[-–—.]?\s*o\s*[-–—.]?\s*e)\b/iy;
const RANGE_AFTER_RE = /\s*(?:to|[-–—/])\s*\d/iy;
const RANGE_BEFORE_RE = /(?:\d\s*(?:to|[-–—/])\s*|\bto\s+)$/i;

/**
 * Blank out every number that denotes experience, leaving the rest in place.
 *
 * The replacement is the same length as what it replaces, so every offset — and
 * therefore every \b in the level patterns — still lines up with the original
 * title.
 *
 *   "ops engineer 2 to 5 years"  ->  "ops engineer # to # years"
 *   "sde-4"                      ->  "sde-4"   (untouched)
 */
function maskExperienceNumbers(text: string): string {
  const out = [...text];
  NUMBER_RE.lastIndex = 0;
  for (const match of text.matchAll(NUMBER_RE)) {
    const start = match.index;
    const end = start + match[0].length;
    YEARS_AFTER_RE.lastIndex = end;
    RANGE_AFTER_RE.lastIndex = end;
    if (
      YEARS_AFTER_RE.test(text) ||
      RANGE_AFTER_RE.test(text) ||
      RANGE_BEFORE_RE.test(text.slice(0, start))
    ) {
      for (let i = start; i < end; i++) out[i] = "#";
    }
  }
  return out.join("");
}

/** The seniority level the title states, or null if it states none. */
export function detectLevel(title: string | null | undefined): number | null {
  const text = (title ?? "").toLowerCase();
  if (!text) return null;

  const levels: number[] = [];
  ROMAN_RE.lastIndex = 0;
  for (const m of text.matchAll(ROMAN_RE)) levels.push(ROMAN_LEVELS[m[1].toLowerCase()]);

  const masked = maskExperienceNumbers(text);
  for (const pattern of DIGIT_LEVEL_RES) {
    pattern.lastIndex = 0;
    for (const m of masked.matchAll(pattern)) levels.push(Number(m[1]));
  }

  // The maximum wins: "SDE II / III" is a III posting.
  return levels.length ? Math.max(...levels) : null;
}

// --- the six tests ---------------------------------------------------------

/**
 * The city list the gate actually reads — notify.match.Preferences.effective_cities.
 *
 * The new column when it has anything, the legacy one otherwise. A migration-window
 * fallback to THIS account's own previous answer, not to somebody else's; the
 * contract slice deletes it along with `cities`. See the Python for the full
 * argument, including why it cannot re-open the blank-account hole.
 */
export function effectiveCities(prefs: Preferences): readonly string[] {
  return prefs.selected_cities.length ? prefs.selected_cities : prefs.cities;
}

/**
 * The posting is fully remote, per the stored jobs.work_mode label.
 *
 * LIVE as of the work-mode slice — notify/description.py writes the column for
 * every job whose description is read, so the branch in cityOk that cities
 * Slice B built now fires. `?? ""` reads NULL and undefined alike as not-remote,
 * deliberately: the branch RELAXES a gate, and treating "not parsed yet" as
 * "fully remote" would drop the city filter for the whole board on the emptiest
 * possible evidence.
 */
export function isRemote(job: MatchableJob): boolean {
  return (job.work_mode ?? "") === REMOTE_WORK_MODE;
}

/** notify/match.work_mode_of — the stored arrangement, NULL read as onsite. */
export function workModeOf(job: MatchableJob): string {
  return (job.work_mode ?? "").trim().toLowerCase() || DEFAULT_WORK_MODE;
}

/**
 * This posting's arrangement is one this account said it would accept.
 *
 * Blank matches nothing — the fourth member of the family that includes an
 * unconfigured experience range and a blank city answer, and unreachable for an
 * onboarded account because PUT /api/preferences refuses an empty selection and
 * is the only writer of this column and of onboarding_completed. See
 * notify.match.work_mode_ok for the argument.
 */
export function workModeOk(job: MatchableJob, prefs: Preferences): boolean {
  return prefs.work_modes.includes(workModeOf(job));
}

/**
 * Three branches — see notify.match.city_ok, which carries the argument.
 *
 *   remote or all-India  ->  anywhere except non-india
 *   an explicit list     ->  membership, minus the two labels that never match
 */
export function cityOk(job: MatchableJob, prefs: Preferences): boolean {
  const city = job.city ?? "";

  if (isRemote(job) || prefs.all_india) return city !== NON_INDIA_LABEL;

  if (CITY_SELECTION_NEVER_MATCHES.has(city)) return false;

  return effectiveCities(prefs).includes(city);
}

export function keywordOk(job: MatchableJob, prefs: Preferences): boolean {
  return matchedTerms(job.title, prefs.keywords).length > 0;
}

export function excluded(job: MatchableJob, prefs: Preferences): boolean {
  return matchedTerms(job.title, prefs.exclusions).length > 0;
}

export function levelExcluded(job: MatchableJob, prefs: Preferences): boolean {
  const level = detectLevel(job.title);
  return level !== null && level > prefs.max_level;
}

/**
 * city AND work mode AND keyword AND NOT excluded AND NOT level — the five free
 * tests. "Free" means "reads a column, costs no request", which is what the seam
 * has always meant; work_mode joined them in the work-mode slice on that basis.
 *
 * notify/match.py additionally carries `worth_a_description`, a WIDER seam used
 * only by crawler/enrich.py to decide what earns a network request. It is
 * deliberately not ported: it reads the location string through
 * fetchers.normalize.india_funnel, the dashboard never enriches anything, and
 * the city labeller is not ported either (see CLAUDE.md, "Parity").
 */
export function titleStageOk(job: MatchableJob, prefs: Preferences): boolean {
  return (
    cityOk(job, prefs) &&
    workModeOk(job, prefs) &&
    keywordOk(job, prefs) &&
    !excluded(job, prefs) &&
    !levelExcluded(job, prefs)
  );
}

export function pmExcluded(job: MatchableJob): boolean {
  return job.is_pm_role === true;
}

/** The stored min_experience as a number, or null for absent/unreadable. */
export function minExperienceOf(job: MatchableJob): number | null {
  const value = job.min_experience;
  if (value === null || value === undefined) return null;
  const years = Number(value);
  return Number.isFinite(years) ? Math.trunc(years) : null;
}

/**
 * The stated minimum is outside this account's looking range.
 *
 * Three branches, in the same order and with the same answers as
 * notify.match.experience_excluded — read that for the reasoning. In short: an
 * unconfigured range excludes everything (never a fallback to the install's 0..2,
 * which would be handing this account somebody else's answer); an unknown minimum
 * defers to `show_no_experience_jobs`; and a confident number must sit inside the
 * range, ends inclusive, which drops a posting for being too junior as well as
 * too senior.
 *
 * THE ORDER MATTERS AND THE PYTHON HAD IT WRONG FIRST. Checking the unknown
 * minimum before the unconfigured range means an account with no range but the
 * toggle on keeps every job whose description states no minimum — 99% of the open
 * board — so "matches nothing" silently becomes "matches almost everything".
 * lib/jobs.experienceClause is the third statement of this rule and must agree
 * with both.
 */
export function experienceExcluded(job: MatchableJob, prefs: Preferences): boolean {
  const { exp_range_low: low, exp_range_high: high } = prefs;
  if (low === null || high === null) return true;

  const years = minExperienceOf(job);
  if (years === null) return !prefs.show_no_experience_jobs;

  return !(low <= years && years <= high);
}

/** The title stage AND NOT a PM role AND NOT an out-of-range role. */
export function isMatch(job: MatchableJob, prefs: Preferences): boolean {
  return titleStageOk(job, prefs) && !pmExcluded(job) && !experienceExcluded(job, prefs);
}

export function selectMatches<T extends MatchableJob>(jobs: T[], prefs: Preferences): T[] {
  return jobs.filter((job) => isMatch(job, prefs));
}

// --- what the screen says about a match ------------------------------------

/**
 * The short note shown beside a matched job, or null when there is nothing
 * worth saying. Presentation only — it never changes whether a job matched.
 *
 * "desc not checked" is the honest reading of desc_checked = false: the job
 * passed every test that reads the title, and the two tests that read a
 * description have no evidence either way. It is shown rather than hidden
 * because "this might still turn out to be a 6-years role" is exactly the
 * caveat a person clicking apply deserves. Since enrichment moved into the
 * crawl it is rare — a job carries it only until the next crawl reaches it.
 *
 * "desc unavailable" is the same caveat for the case that outlives a crawl: we
 * asked the board and got nothing back, so the row is checked but no wiser.
 * Separating the two matters precisely because the reassuring reading — "read,
 * and it said nothing disqualifying" — is the one thing neither of them means.
 */
export function descriptionHint(job: MatchableJob): string | null {
  if (job.desc_checked !== true) return "desc not checked";
  if (job.desc_fetch_failed === true) return "desc unavailable";
  const years = minExperienceOf(job);
  if (years !== null) return `min ${years} yr${years === 1 ? "" : "s"}`;
  return null;
}

/** is_match, with its working shown. Same rules, same order, no second opinion. */
export function explain(job: MatchableJob, prefs: Preferences): { matched: boolean; reason: string } {
  const keywordsHit = matchedTerms(job.title, prefs.keywords);
  const exclusionsHit = matchedTerms(job.title, prefs.exclusions);
  const level = detectLevel(job.title);
  const years = minExperienceOf(job);

  let reason: string;
  if (!cityOk(job, prefs)) {
    // Three ways to fail one gate, worded as notify/match.explain words them —
    // match-parity prints this string when the two disagree, and a reason that
    // reads differently from the Python's makes a real drift harder to see.
    const city = job.city ?? "";
    if (prefs.all_india || isRemote(job))
      reason = `all-India, but ${JSON.stringify(job.city)} is not in India`;
    else if (CITY_SELECTION_NEVER_MATCHES.has(city))
      reason =
        `city ${JSON.stringify(job.city)} never matches an explicit city ` +
        `selection — it is covered by the all-India switch`;
    else reason = `city ${JSON.stringify(job.city)} not in preferences`;
  } else if (!workModeOk(job, prefs)) {
    // Two ways to fail one test, worded exactly as notify/match.explain words
    // them — match-parity prints these strings on a disagreement.
    if (!prefs.work_modes.length) reason = "no work arrangement is selected";
    else
      reason =
        `work mode ${JSON.stringify(workModeOf(job))} is not one of ` +
        prefs.work_modes.map((m) => JSON.stringify(m)).join(", ");
  } else if (!keywordsHit.length) reason = "no keyword in title";
  else if (exclusionsHit.length) reason = `excluded by ${exclusionsHit.join(", ")}`;
  else if (level !== null && level > prefs.max_level)
    reason = `level ${level} above max_level ${prefs.max_level}`;
  else if (pmExcluded(job)) reason = "description frames this as a PM/management role";
  else if (experienceExcluded(job, prefs)) {
    // Four ways to fail one test. Worded exactly as notify/match.explain words
    // them: match-parity prints this string when the two disagree, and a reason
    // that reads differently from the Python's makes a real drift harder to see.
    const { exp_range_low: low, exp_range_high: high } = prefs;
    if (low === null || high === null) reason = "the looking range is not configured";
    else if (years === null)
      reason = "experience unknown, and this account hides jobs stating no minimum";
    else if (years < low) reason = `minimum ${years} years below the looking range ${low}-${high}`;
    else reason = `minimum ${years} years above the looking range ${low}-${high}`;
  } else reason = `matched ${keywordsHit.join(", ")}`;

  return { matched: isMatch(job, prefs), reason };
}
