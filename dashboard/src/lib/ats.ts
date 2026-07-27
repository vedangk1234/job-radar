/**
 * Board probing for the "add a company" flow — a TypeScript port of the parts
 * of fetchers/resolve.py and fetchers/tier1.py the dashboard needs.
 *
 * WHY THIS IS A PORT AND NOT A CALL
 * ---------------------------------
 * The obvious move is for the API route to shell out to `python -c "from
 * fetchers.resolve import resolve"`. It works locally and it dies the moment
 * this folder becomes a Vercel project with its root directory set to
 * dashboard/ — there is no Python there, and the crawler's package is outside
 * the deployment root either way. So the resolution rules exist twice.
 *
 * That duplication is a real cost and worth naming: the token variants, the
 * alias table, and the greenhouse -> lever -> ashby order are now stated in two
 * places, and a change to fetchers/resolve.py will not reach this file on its
 * own. Kept in sync by hand; parity is pinned by scripts/parity-check.ts, which
 * runs both implementations over the same names and diffs the output.
 *
 * What is deliberately NOT ported: fixture archiving (the crawler owns the
 * archive; a dashboard probe is a question, not an observation) and the full
 * job-row normalisation (the probe only needs a count and a few sample titles).
 */

/**
 * Contact address comes from the environment, never the source — mirror of
 * fetchers/http.user_agent(), including the unknown@example.com fallback. This
 * module is imported only from API routes, so process.env resolves server-side;
 * MY_EMAIL is deliberately not NEXT_PUBLIC_, as it must not reach the browser.
 */
const USER_AGENT = `job-radar-dashboard/0.1 (+${
  process.env.MY_EMAIL?.trim() || "unknown@example.com"
})`;

/** Per-request ceiling. A probe that is slow is a probe that is wrong. */
const PROBE_TIMEOUT_MS = 8_000;

export const RESOLVE_ORDER = ["greenhouse", "lever", "ashby"] as const;
export type Tier1Ats = (typeof RESOLVE_ORDER)[number];

/**
 * Tokens no mechanical rule produces, confirmed by hand against the live board.
 * Mirror of ALIASES in fetchers/resolve.py — keep the two identical.
 */
const ALIASES: Record<string, string[]> = {
  razorpay: ["razorpaysoftwareprivatelimited"],
};

/**
 * Port of fetchers/normalize.norm: NFKD, lowercase, punctuation to spaces,
 * whitespace collapsed. `\p{L}\p{N}_` is the Unicode-mode equivalent of
 * Python's `\w`, so accented company names normalise the same way in both.
 *
 * Like the Python original this does NOT strip seniority or suffix words.
 */
export function norm(value: string | null | undefined): string {
  if (value === null || value === undefined) return "";
  return String(value)
    .normalize("NFKD")
    .toLowerCase()
    .replace(/[^\p{L}\p{N}_\s]/gu, " ")
    .replace(/\s+/g, " ")
    .trim();
}

/**
 * Candidate board tokens for a company name, best guess first. Port of
 * fetchers/resolve.token_variants — same base, same suffixes, same order.
 */
export function tokenVariants(companyName: string): string[] {
  const normalised = norm(companyName);
  const base = normalised.replace(/ /g, "");
  if (!base) return [];

  const variants = [base];
  const hyphenated = normalised.replace(/ /g, "-");
  if (hyphenated !== base) variants.push(hyphenated);
  for (const suffix of ["inc", "hq", "technologies"]) variants.push(`${base}${suffix}`);
  variants.push(...(ALIASES[base] ?? []));

  const seen = new Set<string>();
  const ordered: string[] = [];
  for (const token of variants) {
    if (!seen.has(token)) {
      seen.add(token);
      ordered.push(token);
    }
  }
  return ordered;
}

async function getJson(url: string): Promise<unknown | null> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), PROBE_TIMEOUT_MS);
  try {
    const res = await fetch(url, {
      headers: { "User-Agent": USER_AGENT, Accept: "application/json" },
      signal: controller.signal,
      cache: "no-store",
    });
    if (!res.ok) return null;
    return await res.json();
  } catch {
    // A dead token, a timeout and a DNS failure are the same answer here: this
    // is not the board. Matches polite_get's "never raise, return not-ok".
    return null;
  } finally {
    clearTimeout(timer);
  }
}

function rowsOf(payload: unknown, key?: string): Record<string, unknown>[] {
  if (Array.isArray(payload)) {
    return payload.filter((r): r is Record<string, unknown> => !!r && typeof r === "object");
  }
  if (payload && typeof payload === "object" && key) {
    const value = (payload as Record<string, unknown>)[key];
    if (Array.isArray(value)) {
      return value.filter((r): r is Record<string, unknown> => !!r && typeof r === "object");
    }
  }
  return [];
}

const str = (v: unknown): string | null => {
  if (v === null || v === undefined) return null;
  const text = String(v).replace(/\s+/g, " ").trim();
  return text || null;
};

/** The slice of a board a probe reports back: enough to recognise a company. */
export type Probe = { count: number; titles: string[] };

async function probeGreenhouse(token: string): Promise<Probe> {
  const payload = await getJson(
    `https://boards-api.greenhouse.io/v1/boards/${encodeURIComponent(token)}/jobs`,
  );
  const rows = rowsOf(payload, "jobs");
  return { count: rows.length, titles: rows.map((r) => str(r.title) ?? "").filter(Boolean) };
}

async function probeLever(token: string): Promise<Probe> {
  const payload = await getJson(
    `https://api.lever.co/v0/postings/${encodeURIComponent(token)}?mode=json`,
  );
  const rows = rowsOf(payload);
  // Lever's title field is `text`, not `title`.
  return { count: rows.length, titles: rows.map((r) => str(r.text) ?? "").filter(Boolean) };
}

async function probeAshby(token: string): Promise<Probe> {
  const payload = await getJson(
    `https://api.ashbyhq.com/posting-api/job-board/${encodeURIComponent(token)}`,
  );
  const rows = rowsOf(payload, "jobs");
  return { count: rows.length, titles: rows.map((r) => str(r.title) ?? "").filter(Boolean) };
}

const PROBES: Record<Tier1Ats, (token: string) => Promise<Probe>> = {
  greenhouse: probeGreenhouse,
  lever: probeLever,
  ashby: probeAshby,
};

/** Probe one exact (ats, token) pair. Used by the confirm-add step. */
export function probeBoard(ats: Tier1Ats, token: string): Promise<Probe> {
  return PROBES[ats](token);
}

export type Resolved = {
  ats: Tier1Ats;
  token: string;
  jobCount: number;
  sampleTitles: string[];
  /** Every (token, ats) pair tried before the hit, for an honest "no" message. */
  tried: number;
};

/**
 * Return the first live board for a company name, or null.
 *
 * Port of fetchers/resolve.resolve. Two properties are load-bearing and are
 * preserved exactly:
 *
 *   - TOKEN-MAJOR ORDER. The outer loop is the token, the inner one the ATS, so
 *     "acme on lever" beats "acmeinc on greenhouse". Swapping the loops
 *     silently changes which board a name resolves to.
 *   - NON-EMPTY WINS. Some vendors answer 200 with an empty array for tokens
 *     that do not exist, so a bare 200 proves nothing; only jobs prove a board.
 *
 * Sequential rather than parallel, which is also what paces it: one request at
 * a time to one vendor at a time is politer than 18 at once, and the whole
 * search is capped by an overall deadline so a hung vendor cannot hold an HTTP
 * request open indefinitely.
 */
export async function resolve(
  companyName: string,
  { deadlineMs = 25_000 }: { deadlineMs?: number } = {},
): Promise<Resolved | null> {
  const startedAt = Date.now();
  let tried = 0;

  for (const token of tokenVariants(companyName)) {
    for (const ats of RESOLVE_ORDER) {
      if (Date.now() - startedAt > deadlineMs) return null;
      tried += 1;
      const { count, titles } = await PROBES[ats](token);
      if (count > 0) {
        return { ats, token, jobCount: count, sampleTitles: titles.slice(0, 5), tried };
      }
    }
  }
  return null;
}
