/**
 * Workday URL parsing and tenant verification — a TypeScript port of
 * fetchers/resolve.parse_workday_url plus the probe half of
 * fetchers/workday.fetch_workday_detailed.
 *
 * Same duplication caveat as lib/ats.ts: this exists in TypeScript because the
 * dashboard deploys to Vercel without the Python package. Parity with
 * parse_workday_url is pinned by scripts/parity-check.ts.
 *
 * Workday is not guessable the way a Tier 1 board token is — the tenant, host
 * and site name are three independent strings and the site name is whatever the
 * company's HR team typed. So there is no probing by name here: you paste the
 * careers URL you are looking at and this takes it apart.
 */

export const DEFAULT_LANG = "en-US";

// nvidia.wd5.myworkdayjobs.com -> tenant "nvidia"
const WD_HOST = /^([^.]+)\.(wd\d+)\.myworkdayjobs\.com$/i;

// A path segment is a language tag, not a site name, when it looks like en-US
// or en. Workday puts it before the site: /en-US/NVIDIAExternalCareerSite.
// Case-sensitive on the leading pair, exactly like the Python original.
const LANG = /^[a-z]{2}(?:[-_][A-Za-z]{2,4})?$/;

// Segments that are API/app plumbing rather than the site name.
const NOT_A_SITE = new Set(["wday", "cxs", "jobs", "job", "d", "recruiting", "en"]);

export type WorkdayConfig = {
  host: string | null;
  tenant: string | null;
  site: string | null;
  lang: string;
  careers_url: string | null;
  /** Present only when something had to be guessed. Non-empty means do not store. */
  ambiguous?: string[];
};

/**
 * Python's urlsplit, narrowed to the parts this needs. Deliberately not
 * `new URL()`: URL throws on the half-formed strings people actually paste,
 * and parse_workday_url's contract is that it never raises.
 */
function split(raw: string): { netloc: string; path: string } {
  const match = /^(?:[a-zA-Z][a-zA-Z0-9+.-]*:)?(?:\/\/([^/?#]*))?([^?#]*)/.exec(raw);
  return { netloc: match?.[1] ?? "", path: match?.[2] ?? "" };
}

/**
 * A pasted Workday careers URL -> the config dict the fetcher wants.
 *
 * Handles the same four shapes as the Python original:
 *     https://{tenant}.wd{N}.myworkdayjobs.com/{lang}/{site}
 *     https://{tenant}.wd{N}.myworkdayjobs.com/{site}
 *     https://{vanity-host}/{lang}/{site}
 *     .../{lang}/{site}/job/{Location}/{Title}_{REQ}
 *
 * Never throws on a URL it cannot fully understand: it returns what it did
 * parse with `ambiguous` set, because a half-parsed config a human can correct
 * is more useful than an exception. Callers must refuse to store a config whose
 * `ambiguous` list is non-empty.
 */
export function parseWorkdayUrl(careersUrl: string | null | undefined): WorkdayConfig {
  let raw = String(careersUrl ?? "").trim();
  if (raw && !raw.includes("//")) raw = "https://" + raw; // a pasted host with no scheme

  const parts = split(raw);
  const host = (parts.netloc || "").split("@").pop()!.trim().replace(/\/+$/, "").toLowerCase();
  let segments = (parts.path || "").split("/").filter(Boolean);
  const ambiguous: string[] = [];

  // A posting URL carries the site in front of /job/...; drop the posting.
  for (const cut of ["job", "d"]) {
    const at = segments.indexOf(cut);
    if (at !== -1) {
      segments = segments.slice(0, at);
      break;
    }
  }

  // An API URL: /wday/cxs/{tenant}/{site}/jobs
  if (
    segments.length >= 4 &&
    segments[0].toLowerCase() === "wday" &&
    segments[1].toLowerCase() === "cxs"
  ) {
    return {
      host,
      tenant: segments[2],
      site: segments[3],
      lang: DEFAULT_LANG,
      careers_url: `https://${host}/${DEFAULT_LANG}/${segments[3]}`,
    };
  }

  let lang: string | null = null;
  if (segments.length && LANG.test(segments[0])) {
    lang = segments[0];
    segments = segments.slice(1);
  }

  const site = segments.find((s) => !NOT_A_SITE.has(s.toLowerCase())) ?? null;
  if (!site) ambiguous.push("site");

  const hostMatch = WD_HOST.exec(host);
  let tenant: string | null;
  if (hostMatch) {
    tenant = hostMatch[1];
  } else {
    // Custom/vanity domain: the tenant is not in the host. The first label is
    // the best available guess ("careers.acme.com" -> "careers" is wrong,
    // "acme.wd3.custom.com" -> "acme" is right), so flag it either way and let
    // a human confirm against the /wday/cxs/ URL the site itself calls.
    const labels = host.split(".").filter(Boolean);
    tenant = labels[0] ?? null;
    if (!host) ambiguous.push("host");
    ambiguous.push("tenant");
  }

  const config: WorkdayConfig = {
    host: host || null,
    tenant,
    site,
    lang: lang || DEFAULT_LANG,
    careers_url: raw || null,
  };
  if (ambiguous.length) config.ambiguous = ambiguous;
  return config;
}

// --- tenant verification ---------------------------------------------------
//
// One unfiltered POST, which answers two questions at once: is this actually a
// board, and does the tenant expose an India facet (which is what
// companies.india_scoped records).

const HEADERS = { "Content-Type": "application/json", Accept: "application/json" };
const PROBE_TIMEOUT_MS = 15_000;

/** Exact match only. "Indiana" is a US state, not a typo — see workday.py note 2. */
function isIndia(descriptor: unknown): boolean {
  return String(descriptor ?? "").trim().toLowerCase() === "india";
}

type Facet = Record<string, unknown>;

/**
 * Yield every facet in the tree, including nested groups. `locationMainGroup`
 * is a group whose `values` are themselves facets, so a flat scan of the top
 * level misses the country facet on every tenant that uses one.
 */
function* walkFacets(nodes: unknown): Generator<Facet> {
  if (!Array.isArray(nodes)) return;
  for (const node of nodes) {
    if (!node || typeof node !== "object") continue;
    const facet = node as Facet;
    const values = facet.values;
    const nested = Array.isArray(values)
      ? values.filter((v) => v && typeof v === "object" && (v as Facet).facetParameter)
      : [];
    if (nested.length) yield* walkFacets(nested);
    else yield facet;
  }
}

/**
 * The tenant's own India facet, discovered by VALUE rather than by name — the
 * facet is called `locationCountry` on Adobe, `locationHierarchy1` on NVIDIA
 * and something 70 characters long on Salesforce. Prefers a facet that calls
 * itself Country when the tenant has more than one.
 */
export function findIndiaFacet(payload: unknown): { param: string; wid: string } | null {
  const facets = payload && typeof payload === "object" ? (payload as Facet).facets : null;
  const candidates: { rank: number; param: string; wid: string }[] = [];

  for (const facet of walkFacets(facets)) {
    const param = facet.facetParameter;
    if (typeof param !== "string" || !param) continue;
    const values = Array.isArray(facet.values) ? facet.values : [];
    for (const value of values) {
      if (!value || typeof value !== "object") continue;
      const v = value as Facet;
      if (!isIndia(v.descriptor)) continue;
      const looksCountry =
        String(facet.descriptor ?? "").toLowerCase().includes("country") ||
        param.toLowerCase().includes("country");
      const wid = String(v.id ?? "");
      if (wid) candidates.push({ rank: looksCountry ? 0 : 1, param, wid });
    }
  }
  if (!candidates.length) return null;
  candidates.sort((a, b) => a.rank - b.rank);
  return { param: candidates[0].param, wid: candidates[0].wid };
}

export type WorkdayProbe = {
  boardTotal: number;
  /** An India facet exists at source, so the crawler's fetch will be scoped. */
  indiaScoped: boolean;
  facet: string | null;
  sampleTitles: string[];
};

export class WorkdayError extends Error {}

/**
 * One unfiltered page against {host}/wday/cxs/{tenant}/{site}/jobs.
 *
 * Failure semantics copied from workday.py: a 404, an HTML error page, or a
 * response with no total/jobPostings RAISES, because "the tenant is wrong" must
 * never be storable as "the board is empty". total=0 on a well-formed response
 * is a real, empty board and is not an error.
 *
 * Not ported: the blind-WID fallback. When no facet is discovered this reports
 * indiaScoped=false, which is the honest and conservative answer — the crawler
 * then filters that tenant client-side by city label, exactly as it does today
 * for any row stored with india_scoped=false.
 */
export async function probeWorkday(config: WorkdayConfig): Promise<WorkdayProbe> {
  const host = (config.host ?? "").trim().replace(/\/+$/, "");
  const site = (config.site ?? "").trim().replace(/^\/+|\/+$/g, "");
  const tenant = (config.tenant ?? "").trim();
  if (!host || !site || !tenant) {
    throw new WorkdayError(
      `needs host, tenant and site; got host=${host || "?"} tenant=${tenant || "?"} site=${site || "?"}`,
    );
  }

  const url = `https://${host}/wday/cxs/${tenant}/${site}/jobs`;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), PROBE_TIMEOUT_MS);

  let payload: unknown;
  try {
    const res = await fetch(url, {
      method: "POST",
      headers: HEADERS,
      body: JSON.stringify({ appliedFacets: {}, limit: 20, offset: 0, searchText: "" }),
      signal: controller.signal,
      cache: "no-store",
    });
    if (!res.ok) throw new WorkdayError(`${url}: HTTP ${res.status}`);
    payload = await res.json();
  } catch (err) {
    if (err instanceof WorkdayError) throw err;
    throw new WorkdayError(`${url}: ${err instanceof Error ? err.message : String(err)}`);
  } finally {
    clearTimeout(timer);
  }

  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new WorkdayError(`${url}: response was not a JSON object`);
  }
  const body = payload as Record<string, unknown>;
  if (!("total" in body) && !("jobPostings" in body)) {
    throw new WorkdayError(
      `${url}: no total/jobPostings in the response (keys: ${Object.keys(body).slice(0, 6).join(", ")}) ` +
        `— this is an error page or the wrong site name, not a board`,
    );
  }
  const postings = Array.isArray(body.jobPostings) ? body.jobPostings : [];

  const found = findIndiaFacet(body);
  return {
    boardTotal: typeof body.total === "number" ? body.total : 0,
    indiaScoped: !!found,
    facet: found?.param ?? null,
    sampleTitles: postings
      .slice(0, 5)
      .map((p) => (p && typeof p === "object" ? String((p as Facet).title ?? "") : ""))
      .filter(Boolean),
  };
}
