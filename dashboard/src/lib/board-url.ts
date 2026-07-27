/**
 * A pasted URL -> (ats, token, config).
 *
 * The "add by URL" path exists because `resolve()` only guesses tokens from a
 * company NAME, and two kinds of board defeat that: a Workday tenant (three
 * independent strings, none of them derivable) and any company whose board
 * token is not a mechanical variant of its name. For both, the URL you are
 * already looking at holds the answer.
 *
 * Dashboard-only glue — no Python counterpart. The Workday half delegates to
 * the port in lib/workday.ts; this file only decides which vendor a URL belongs
 * to and where the token sits in its path.
 */

import type { Tier1Ats } from "./ats";
import { parseWorkdayUrl, type WorkdayConfig } from "./workday";

export type BoardUrl =
  | { kind: "tier1"; ats: Tier1Ats; token: string }
  | { kind: "workday"; config: WorkdayConfig }
  | { kind: "unknown"; reason: string };

/**
 * Where the token sits, per vendor host. Ordered so the API host is tested
 * before the public one — `boards-api.greenhouse.io` also ends in
 * `greenhouse.io`, and matching the public rule first would take `v1` as the
 * token.
 */
const TIER1_RULES: {
  ats: Tier1Ats;
  host: RegExp;
  /** Pull the token out of the path segments, or a query parameter. */
  token: (segments: string[], params: URLSearchParams) => string | null;
}[] = [
  {
    ats: "greenhouse",
    host: /^boards-api\.greenhouse\.io$/i,
    // /v1/boards/{token}/jobs
    token: (s) => (s[0]?.toLowerCase() === "v1" && s[1]?.toLowerCase() === "boards" ? s[2] ?? null : null),
  },
  {
    ats: "greenhouse",
    host: /(^|\.)greenhouse\.io$/i,
    // boards.greenhouse.io/{token}, job-boards.greenhouse.io/{token},
    // and the iframe embed ...?for={token}
    token: (s, p) => p.get("for") || s[0] || null,
  },
  {
    ats: "lever",
    host: /^api\.lever\.co$/i,
    // /v0/postings/{token}
    token: (s) => (s[0]?.toLowerCase() === "v0" && s[1]?.toLowerCase() === "postings" ? s[2] ?? null : null),
  },
  {
    ats: "lever",
    host: /(^|\.)lever\.co$/i,
    // jobs.lever.co/{token}[/{posting-id}]
    token: (s) => s[0] ?? null,
  },
  {
    ats: "ashby",
    host: /^api\.ashbyhq\.com$/i,
    // /posting-api/job-board/{token}
    token: (s) =>
      s[0]?.toLowerCase() === "posting-api" && s[1]?.toLowerCase() === "job-board" ? s[2] ?? null : null,
  },
  {
    ats: "ashby",
    host: /(^|\.)ashbyhq\.com$/i,
    // jobs.ashbyhq.com/{token}[/{posting-id}]
    token: (s) => s[0] ?? null,
  },
];

export function parseBoardUrl(input: string): BoardUrl {
  let raw = String(input ?? "").trim();
  if (!raw) return { kind: "unknown", reason: "no URL given" };
  if (!raw.includes("//")) raw = "https://" + raw;

  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    return { kind: "unknown", reason: "that is not a URL I can parse" };
  }

  const host = url.hostname.toLowerCase();
  const segments = url.pathname.split("/").filter(Boolean);

  // Workday first: its URLs can be vanity domains that match nothing else, and
  // the /wday/cxs/ API path is unambiguous wherever it is hosted.
  if (/\.myworkdayjobs\.com$/i.test(host) || segments.some((s) => s.toLowerCase() === "wday")) {
    return { kind: "workday", config: parseWorkdayUrl(raw) };
  }

  for (const rule of TIER1_RULES) {
    if (!rule.host.test(host)) continue;
    const token = rule.token(segments, url.searchParams);
    if (token) return { kind: "tier1", ats: rule.ats, token };
    return {
      kind: "unknown",
      reason: `that looks like a ${rule.ats} URL but I could not find the board token in it`,
    };
  }

  return {
    kind: "unknown",
    reason:
      `unrecognised board host "${host}". Supported: greenhouse, lever, ashby and Workday. ` +
      `For a Workday tenant, paste the careers URL itself (…myworkdayjobs.com/en-US/SiteName).`,
  };
}
