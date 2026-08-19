import type { CompanyCard } from "./companies";
import { cityChipLabel, cityDisplayName } from "./labels";

/**
 * The card grid's pure functions: what a card SAYS, and what order the cards
 * come in. No database, no clock of its own, no React.
 *
 * WHY THEY ARE HERE AND NOT IN THE COMPONENTS THAT RENDER THEM. Two reasons, and
 * both are the reason lib/badges.ts and lib/match.ts are their own files.
 *
 * The first is the bundle rule this project already enforces: a client component
 * that imports a VALUE drags that module's whole import graph into the browser,
 * so anything a card component calls must reach no database. The `CompanyCard`
 * import below is `import type` and is erased; lib/labels imports nothing at all.
 *
 * The second is that `npm run ui-check` has no browser. The sort order, the
 * default the dropdown starts on, the freshness ladder and the chip arithmetic
 * are all decisions with an off-by-one in them, and a function is checkable while
 * a JSX expression buried in a component is not. Every export here is asserted
 * directly by that script.
 */

/**
 * HOW MANY CARDS HOME SHOWS, AND THE RULE IS "ALWAYS FULL".
 *
 * The grid is never an empty state. Companies with at least one relevant role
 * come first, and the rest of the slots are the catalog in alphabetical order,
 * shown honestly as `0 relevant roles` — the filler is real companies we really
 * crawl, not placeholders. Fifty is the whole catalog today, so nothing is cut.
 *
 * SELECTION AND ORDER ARE TWO DIFFERENT DECISIONS, which matters the day the
 * catalog outgrows this number. The fifty shown are ALWAYS chosen by
 * relevance-then-name (lib/companies.byMatchesThenName); the dropdown reorders
 * those fifty and never re-selects them. Sorting first and slicing second would
 * let "Latest" — under which every zero-relevance company ties at null — decide
 * WHICH companies you can see, and a company with matches would drop off the page
 * because it had nothing new today.
 */
export const GRID_CAP = 50;

/** The three orders the dropdown offers. */
export type SortMode = "latest" | "count" | "name";

export const SORT_OPTIONS: ReadonlyArray<{ value: SortMode; label: string }> = [
  { value: "latest", label: "Latest relevant role" },
  { value: "count", label: "Highest relevant count" },
  { value: "name", label: "Alphabetical A–Z" },
];

/**
 * WHICH ORDER HOME OPENS ON, DERIVED RATHER THAN STORED.
 *
 * Highest-count-first is the browsing order — it answers "where is there
 * anything for me at all", which is the question on a screen you have not looked
 * at before. Latest-first answers a different one — "what changed" — and it is
 * only worth the top slot once there is something that changed.
 *
 * SO THE TRIGGER IS THE QUEUE, WHICH IS THE NEW BADGE ASKED PER COMPANY. A card's
 * `queued` counts its matches carrying no user_job_notifications row for this
 * account, which is exactly lib/badges.homeBadge's condition for NEW. Home
 * therefore sorts newest-first precisely while there is a NEW badge somewhere on
 * it, and a person can see that the rule is being followed rather than having to
 * trust it.
 *
 * IT IS NOT MONOTONIC, AND THAT IS ACCEPTED RATHER THAN OVERLOOKED. When the
 * hour's digest sends, those rows get receipts, `queued` falls to zero and the
 * default returns to highest-first — the page goes back to browsing order once
 * there is nothing new in it, which is honest. Making it one-way would take a
 * `preferences` column to remember that a first match ever happened, and this
 * slice adds no schema; deriving it from `first_seen_at > preferences.updated_at`
 * would survive the digest but reset on every filter save. Both were considered
 * and this one needs no new fact in the world.
 *
 * It inherits one quirk from the badge it shares a predicate with: a matching
 * SEEDED job has no notification row either, so it reads as queued although no
 * digest will ever send it. That is the NEW badge's own long-standing edge, not a
 * new one — the two agree, which is the property worth keeping.
 */
export function defaultSortMode(cards: readonly CompanyCard[]): SortMode {
  return cards.some((card) => card.queued > 0) ? "latest" : "count";
}

/**
 * Reorder the cards. Total and deterministic: every comparator ends in a name
 * comparison, so two cards can never swap places between renders.
 *
 * `latest` puts companies with no relevant role at all LAST rather than first,
 * which `null` sorting naively would not. One genuinely new posting floats its
 * whole company to slot 1 — that is the point of the mode, and why the card
 * carries the freshest match rather than an average of anything.
 */
export function sortCards(cards: readonly CompanyCard[], mode: SortMode): CompanyCard[] {
  const byName = (a: CompanyCard, b: CompanyCard) => a.name.localeCompare(b.name);
  const next = [...cards];

  if (mode === "name") return next.sort(byName);
  if (mode === "count") return next.sort((a, b) => b.match_jobs - a.match_jobs || byName(a, b));

  return next.sort((a, b) => {
    const left = a.latest_match_at;
    const right = b.latest_match_at;
    if (left === null && right === null) return byName(a, b);
    if (left === null) return 1;
    if (right === null) return -1;
    // ISO 8601 UTC strings, so lexicographic order IS chronological order. See
    // lib/companies.toIso for why every timestamp on a card is normalised to
    // that spelling before it gets here.
    return right.localeCompare(left) || byName(a, b);
  });
}

/**
 * HOW FRESH THE FRESHEST RELEVANT ROLE IS, in words.
 *
 * `now` is a parameter rather than a `new Date()` inside, for the reason every
 * matcher in this project takes its inputs: a function that reads the clock
 * cannot be checked, and this one is rendered on the SERVER — the page is
 * `force-dynamic`, so the string is computed once against the server's clock and
 * shipped as text. Formatting it in the browser instead would put a second clock
 * on the screen and hydrate differently from the markup it replaced.
 *
 * THE LADDER, AND WHY THE FIRST RUNG IS AN HOUR. The crawl is hourly, so a match
 * under an hour old arrived in the most recent cycle — it is as new as anything
 * in this system can be, and "new today" is what that is worth saying. After that
 * the reading is elapsed time, which is what a person actually wants from a
 * posting they are deciding whether to open, and only past a week does an
 * absolute date become the more useful answer.
 *
 * Returns null for a company with no relevant role: there is no freshness to
 * report, and printing "never" beside a zero would be shouting about a company
 * that simply has nothing for you today.
 */
export function freshnessLabel(iso: string | null, now: Date = new Date()): string | null {
  if (!iso) return null;
  const seen = new Date(iso);
  if (Number.isNaN(seen.getTime())) return null;

  const minutes = Math.floor((now.getTime() - seen.getTime()) / 60000);
  // A crawl a few seconds ahead of this render, or a clock that has drifted the
  // wrong way, must not print "-1h ago".
  if (minutes < 60) return "new today";
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days === 1) return "yesterday";
  if (days < 7) return `${days}d ago`;

  const sameYear = seen.getUTCFullYear() === now.getUTCFullYear();
  return seen.toLocaleDateString("en-GB", {
    day: "numeric",
    month: "short",
    ...(sameYear ? {} : { year: "numeric" }),
    timeZone: "UTC",
  });
}

/** One city chip: the shouted label, the friendly name for its tooltip, the count. */
export type CityChip = { label: string; name: string; count: number };

/**
 * THE CHIP ROW: "18 roles · 5 BLR · 4 HYD · 4 PUNE · +more".
 *
 * Every number here is inside this account's filter, which is a property of where
 * the counts come from rather than a rule applied here — a job outside the city
 * answer never reaches lib/jobs.matchCountsByCompany at all, so it cannot reach a
 * chip. The one case worth knowing about is a REMOTE posting, which matches
 * anywhere in India and therefore brings its own real city onto the chip row even
 * if that city was not ticked. It is a real match in a real place and it gets its
 * real label.
 *
 * TIES BREAK ALPHABETICALLY, so the row is stable between renders and between
 * two companies with the same spread. `total` is the card's big number and may
 * exceed the three chips shown; `more` is what the rest is called.
 */
export function topCityChips(
  cityCounts: Readonly<Record<string, number>>,
  limit = 3,
): { chips: CityChip[]; more: number; total: number } {
  const entries = Object.entries(cityCounts).filter(([, count]) => count > 0);
  const total = entries.reduce((n, [, count]) => n + count, 0);

  const ranked = entries.sort(
    ([leftLabel, leftCount], [rightLabel, rightCount]) =>
      rightCount - leftCount || leftLabel.localeCompare(rightLabel),
  );

  return {
    chips: ranked.slice(0, limit).map(([label, count]) => ({
      label: cityChipLabel(label),
      name: cityDisplayName(label),
      count,
    })),
    more: Math.max(0, ranked.length - limit),
    total,
  };
}

/**
 * The expanded card's job list, grouped by city — ALL of them, not the three the
 * chip row had room for. Same order as the chips (most first, ties
 * alphabetically), so opening a card does not reshuffle what you were reading.
 *
 * Generic over the row type so this file needs no `JobView` import: the grouping
 * reads one field, and taking the whole type for it would drag lib/jobs — and
 * therefore postgres.js — toward the browser bundle.
 *
 * A NULL city is grouped rather than dropped. It should not exist (see
 * lib/jobs.cityClause on why an unlabelled row is a bug rather than a doubt), and
 * the honest failure is a group called "—" that somebody notices, not a job that
 * silently vanishes out of a list whose count came from a different query.
 */
export function groupJobsByCity<T extends { city: string | null }>(
  jobs: readonly T[],
): Array<{ label: string; name: string; jobs: T[] }> {
  const groups = new Map<string, T[]>();
  for (const job of jobs) {
    const key = job.city ?? "";
    const found = groups.get(key);
    if (found) found.push(job);
    else groups.set(key, [job]);
  }

  return [...groups.entries()]
    .sort(([leftKey, left], [rightKey, right]) =>
      right.length - left.length || leftKey.localeCompare(rightKey),
    )
    .map(([key, rows]) => ({
      label: key ? cityChipLabel(key) : "—",
      name: key ? cityDisplayName(key) : "no city label",
      jobs: rows,
    }));
}

/**
 * The letters on a company's placeholder logo.
 *
 * TWO LETTERS FROM TWO WORDS, ONE FROM ONE — "Tata Consultancy" is TC and
 * "Adobe" is A, because a monogram's job is to be told apart at 40px from the
 * eleven other cards on its row, and `AD` is no more distinguishable from `AT`
 * than `A` is. Words that are not letters at all (a leading "7", an ampersand)
 * are skipped rather than rendered, so a name like "Acme & Co" reads AC.
 *
 * SLICE 3 REPLACES WHAT IS DRAWN, NOT THIS. When real logos land, this stays as
 * the fallback for the companies whose fetch found nothing — which is why it is a
 * function of the name alone and reaches no network, no cache and no column.
 */
export function monogram(name: string): string {
  const words = name
    .split(/[^\p{L}\p{N}]+/u)
    .filter(Boolean)
    .filter((word) => /^\p{L}/u.test(word));
  if (words.length === 0) return "?";
  if (words.length === 1) return words[0].slice(0, 1).toUpperCase();
  return (words[0].slice(0, 1) + words[1].slice(0, 1)).toUpperCase();
}

/**
 * Which of the placeholder tints a company gets — stable for a name, so a card
 * does not change colour between two loads of the same page.
 *
 * A sum of code points rather than a real hash: the input is a company name and
 * the output picks one of six CSS classes, so the only property that matters is
 * that it is a pure function of the string. Nothing depends on the distribution
 * being good and nothing may ever depend on it being unguessable.
 */
export function tintIndex(name: string, buckets = 6): number {
  let total = 0;
  for (let i = 0; i < name.length; i += 1) total = (total + name.charCodeAt(i)) % 4096;
  return total % buckets;
}
