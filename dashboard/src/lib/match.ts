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
 * min_experience = NULL, and both of those already mean KEEP — the inclusion
 * bias the Python module states outright. So "description never read" and
 * "description read, nothing disqualifying found" produce the same verdict, and
 * the dashboard shows the same set either way. What the dashboard does extra is
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

export type Preferences = {
  cities: readonly string[];
  keywords: readonly string[];
  exclusions: readonly string[];
  max_level: number;
};

/** Anything with the columns the rules read. Both job shapes in the app satisfy it. */
export type MatchableJob = {
  title?: string | null;
  city?: string | null;
  min_experience?: number | null;
  is_pm_role?: boolean | null;
  desc_checked?: boolean | null;
  desc_fetch_failed?: boolean | null;
};

/**
 * The ceiling on what a description may demand. A constant here because it is a
 * constant there — notify/match.MAX_MIN_EXPERIENCE. If it ever becomes a
 * preferences column it becomes one in both files on the same day.
 */
export const MAX_MIN_EXPERIENCE = 2;

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

export function cityOk(job: MatchableJob, prefs: Preferences): boolean {
  return prefs.cities.includes(job.city ?? "");
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

/** city AND keyword AND NOT excluded AND NOT level — the four title tests. */
export function titleStageOk(job: MatchableJob, prefs: Preferences): boolean {
  return (
    cityOk(job, prefs) &&
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

export function experienceExcluded(job: MatchableJob): boolean {
  const years = minExperienceOf(job);
  return years !== null && years > MAX_MIN_EXPERIENCE;
}

/** The title stage AND NOT a PM role AND NOT a 3+-years role. */
export function isMatch(job: MatchableJob, prefs: Preferences): boolean {
  return titleStageOk(job, prefs) && !pmExcluded(job) && !experienceExcluded(job);
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
  if (!cityOk(job, prefs)) reason = `city ${JSON.stringify(job.city)} not in preferences`;
  else if (!keywordsHit.length) reason = "no keyword in title";
  else if (exclusionsHit.length) reason = `excluded by ${exclusionsHit.join(", ")}`;
  else if (level !== null && level > prefs.max_level)
    reason = `level ${level} above max_level ${prefs.max_level}`;
  else if (pmExcluded(job)) reason = "description frames this as a PM/management role";
  else if (experienceExcluded(job))
    reason = `minimum ${years} years above ceiling ${MAX_MIN_EXPERIENCE}`;
  else reason = `matched ${keywordsHit.join(", ")}`;

  return { matched: isMatch(job, prefs), reason };
}
