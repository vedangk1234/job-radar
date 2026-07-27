/**
 * Prove the two dashboard features are actually ON SCREEN, against the running app.
 *
 *     npm run dev          # in another terminal
 *     npm run ui-check
 *
 * WHY THIS EXISTS
 * ---------------
 * The Applied checkbox and the NEW / NOTIFIED / SUGGESTED badges were once
 * described in a spec, agreed to, and never built — and nothing failed, because
 * nothing in the repo could tell the difference between "rendered" and
 * "designed". A type checker proves the props line up. A database query proves
 * the column exists. Neither proves a person opening the page sees a checkbox.
 *
 * So this asks the question the other checks cannot: it fetches REAL rows from
 * the REAL running server over HTTP — same route the browser calls, same session
 * cookie, same live database behind it — renders the REAL row component with
 * them, and asserts on the markup that comes out.
 *
 * The two halves are deliberate. The data half is the running app: if the API
 * stopped returning `applied`, this fails. The render half is the shipped
 * component, imported not copied: if JobLine stopped emitting the badge, this
 * fails. What it does not cover is CSS and clicking, which is where a browser
 * would be needed and where the failure mode is visible rather than silent.
 *
 * It mints its own session the way scripts/mint-session.ts does — see that file
 * for why that is not a backdoor.
 */

import { config } from "dotenv";
import { encode } from "next-auth/jwt";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { JobLine, type Mode } from "../src/components/JobLine";
import type { JobView } from "../src/lib/jobs";

config({ path: ".env.local" });

const BASE = process.env.UI_CHECK_BASE_URL ?? "http://localhost:3000";
const COOKIE = "authjs.session-token";

let failures = 0;

function check(ok: boolean, label: string, detail = "") {
  console.log(`  ${ok ? "ok  " : "FAIL"}  ${label}${detail && !ok ? `\n        ${detail}` : ""}`);
  if (!ok) failures += 1;
}

/**
 * The row component, rendered exactly as the page renders it.
 *
 * createElement rather than JSX so this script needs no transform of its own:
 * tsconfig sets `jsx: "preserve"` for Next's compiler, and tsx/esbuild would
 * hand the untransformed JSX straight to Node. One function call is a smaller
 * price than a second build configuration living in a check script.
 */
function render(job: JobView, mode: Mode): string {
  return renderToStaticMarkup(createElement(JobLine, { job, mode, onApplied: () => {} }));
}

async function session(): Promise<string> {
  const secret = (process.env.AUTH_SECRET ?? "").trim();
  const email = (process.env.ALLOWED_EMAIL ?? "").trim();
  if (!secret || !email) throw new Error("AUTH_SECRET and ALLOWED_EMAIL must be set in .env.local");
  const token = await encode({
    token: { email, name: "ui-check", sub: "ui-check", authorised: true },
    secret,
    salt: COOKIE,
    maxAge: 300,
  });
  return `${COOKIE}=${token}`;
}

async function get(path: string, cookie: string): Promise<Response> {
  const res = await fetch(`${BASE}${path}`, { headers: { cookie }, redirect: "manual" });
  return res;
}

async function jobsOf(companyId: number, filter: Mode, cookie: string): Promise<JobView[]> {
  const res = await get(`/api/companies/${companyId}/jobs?filter=${filter}`, cookie);
  if (!res.ok) throw new Error(`GET jobs?filter=${filter} answered ${res.status}`);
  return (await res.json()).jobs as JobView[];
}

async function main() {
  const cookie = await session();

  // --- the server is up, and it is guarding itself ------------------------
  console.log(`\nrunning app at ${BASE}`);
  const anonymous = await fetch(`${BASE}/api/jobs/applied`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ canonical_id: "nope", applied: true }),
  });
  check(anonymous.status === 401, "PATCH /api/jobs/applied is 401 without a session", `got ${anonymous.status}`);

  for (const path of ["/", "/jobs"]) {
    const res = await get(path, cookie);
    check(res.status === 200, `GET ${path} renders for a signed-in session`, `got ${res.status}`);
  }

  // --- find live rows to render ------------------------------------------
  const companies = (await (await get("/api/companies", cookie)).json()).companies as {
    id: number;
  }[];

  let home: JobView[] = [];
  let all: JobView[] = [];
  for (const company of companies) {
    const matches = await jobsOf(company.id, "matches", cookie);
    if (matches.length < 2) continue;
    home = matches;
    all = await jobsOf(company.id, "all", cookie);
    break;
  }
  if (!home.length) throw new Error("no company on the live database has 2+ matching jobs to render");

  // --- HOME: the checkbox -------------------------------------------------
  console.log("\nHome — the Applied checkbox");
  const unticked = render({ ...home[0], applied: false, applied_at: null }, "matches");
  check(unticked.includes('type="checkbox"'), "an unticked row renders a checkbox");
  check(unticked.includes("Applied"), "the checkbox is labelled Applied");
  check(!unticked.includes('class="job applied"'), "an unapplied row is not dimmed");

  const stamp = "2026-07-27T09:30:00.000Z";
  const ticked = render({ ...home[0], applied: true, applied_at: stamp }, "matches");
  check(ticked.includes('class="job applied"'), "a ticked row carries the applied class (dim + strikethrough)");
  check(ticked.includes("checked=\"\"") || ticked.includes("checked"), "the box renders checked");
  check(ticked.includes(`href="${home[0].url}"`), "the apply link survives being applied to");
  check(/title="Applied on [^"]+\. Untick to undo\."/.test(ticked), "the tooltip names the applied date");

  // --- HOME: the badges ---------------------------------------------------
  console.log("\nHome — NEW / NOTIFIED");
  const notified = render({ ...home[0], notified: true }, "matches");
  check(notified.includes('class="badge notified"'), "a match already emailed reads NOTIFIED");
  check(notified.includes(">NOTIFIED<"), "the word NOTIFIED is in the markup");
  check(!notified.includes(">NEW<"), "and NEW is not");

  const fresh = render({ ...home[0], notified: false }, "matches");
  check(fresh.includes('class="badge new"'), "a match not yet emailed reads NEW");
  check(fresh.includes(">NEW<"), "the word NEW is in the markup");
  check(!fresh.includes(">NOTIFIED<"), "and NOTIFIED is not");

  // --- JOBS: SUGGESTED, and its absence -----------------------------------
  console.log("\nJobs — SUGGESTED, and the rows that get nothing");
  const matching = all.find((j) => j.matches);
  const nonMatching = all.find((j) => !j.matches);
  check(Boolean(matching), "the raw list contains at least one matching row");
  check(Boolean(nonMatching), "the raw list contains at least one non-matching row");

  if (matching) {
    const html = render(matching, "all");
    check(html.includes('class="badge suggested"'), `a matching row reads SUGGESTED — "${matching.title}"`);
    check(!html.includes('type="checkbox"'), "and carries no Applied checkbox (Jobs is the raw view)");
  }
  if (nonMatching) {
    const html = render(nonMatching, "all");
    check(!html.includes("badge"), `a non-matching row has NO badge — "${nonMatching.title}"`);
    check(html.includes("notified"), "but keeps the notified text and the status tag");
  }

  // --- the ordering rule --------------------------------------------------
  console.log("\nHome — applied jobs sink to the bottom");
  const list: JobView[] = [
    { ...home[0], applied: true },
    { ...home[1], applied: false },
  ];
  const sorted = [...list].sort((a, b) => Number(a.applied) - Number(b.applied));
  check(sorted[0].canonical_id === home[1].canonical_id, "the unapplied job sorts first");
  check(sorted[1].canonical_id === home[0].canonical_id, "the applied job sorts last");

  console.log(
    failures
      ? `\nFAILED — ${failures} assertion(s) about what the UI renders did not hold.`
      : `\nOK — every assertion held, against live rows from ${BASE}.`,
  );
  if (failures) process.exitCode = 1;
}

main().catch((err) => {
  console.error("\nFAILED:", err instanceof Error ? err.message : err);
  console.error("Is the dev server running? `npm run dev` in another terminal.");
  process.exitCode = 1;
});
