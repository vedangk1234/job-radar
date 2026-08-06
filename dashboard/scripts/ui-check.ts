/**
 * Prove the dashboard features are actually ON SCREEN, and that the gate is shut.
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
 * HOW PHASE 2a CHANGED IT, and this is the part worth reading before editing.
 * The Phase-1 version fetched rows over HTTP carrying a session cookie it MINTED
 * ITSELF, with scripts/mint-session.ts encrypting a token using AUTH_SECRET and
 * next-auth's own `encode`. That worked because an Auth.js JWT session is a
 * self-contained cookie: hold the signing secret and you can manufacture a
 * caller. A Supabase session is not that. It is a pair of tokens issued by
 * GoTrue, and the equivalent trick needs the service-role key — a credential
 * that would then have to exist in .env.local, be read by a script in this
 * repo, and mint arbitrary identities. That is a strictly larger blast radius
 * than the thing it was buying.
 *
 * So the two halves were separated instead, and each is now done the honest way:
 *
 *   DATA — live rows come from the same lib functions the server components
 *   call (lib/jobs, lib/preferences), against the live database, for the
 *   operator's own user id. No forged session, no service-role key, nothing
 *   that could become a backdoor. It is the same data the page would render,
 *   fetched by the same code, one layer below HTTP.
 *
 *   THE GATE — asserted over real HTTP against the running server with NO
 *   cookie at all, which is the case that actually matters now that anyone can
 *   sign up. Phase 1 checked one route was 401; this checks every route and
 *   every page, because 2a rewrote the middleware matcher and the guard, and a
 *   matcher is exactly the kind of thing that develops a hole.
 *
 * What it still does not cover is CSS and clicking, which is where a browser
 * would be needed and where the failure mode is visible rather than silent.
 */

import { config } from "dotenv";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { JobLine, type Mode } from "../src/components/JobLine";
import type { JobView } from "../src/lib/jobs";

config({ path: ".env.local" });

/**
 * The app's data modules are imported LATE, and that is not a style choice.
 * src/lib/db.ts builds its pool at module scope, and static imports are hoisted
 * above the loadEnv() call above — so importing lib/db, lib/jobs, lib/companies,
 * lib/preferences or scripts/current-user (which imports lib/db itself) at the
 * top evaluates dashboardDbUrl() against an empty process.env and throws
 * `DASHBOARD_DB_URL is not set` before a single assertion runs.
 * scripts/badge-check.ts and scripts/config-roundtrip.ts defer for the same
 * reason and document it the same way.
 *
 * JobLine stays a static import: it reaches lib/badges and lib/match, neither of
 * which touches the database, and its lib/jobs import is type-only and erased.
 */
type Lib = {
  sql: typeof import("../src/lib/db").sql;
  currentUserId: typeof import("./current-user").currentUserId;
  matchingJobsForCompany: typeof import("../src/lib/jobs").matchingJobsForCompany;
  openJobsWithMatchFlag: typeof import("../src/lib/jobs").openJobsWithMatchFlag;
  listCompanyCards: typeof import("../src/lib/companies").listCompanyCards;
  loadPreferences: typeof import("../src/lib/preferences").loadPreferences;
  /**
   * DEFERRED FOR THE SAME REASON AS THE REST, even though it is a component and
   * JobLine above is a static import. JobLine reaches only lib/badges and
   * lib/match, neither of which touches the database. DeleteAccount imports the
   * server action, which imports lib/account, which imports lib/db — so a static
   * import here would build the pool before loadEnv() and throw
   * `DASHBOARD_DB_URL is not set` before the first assertion.
   */
  DeleteAccount: typeof import("../src/components/DeleteAccount").DeleteAccount;
};

async function loadLib(): Promise<Lib> {
  const [db, currentUser, jobs, companies, preferences, deleteAccount] = await Promise.all([
    import("../src/lib/db"),
    import("./current-user"),
    import("../src/lib/jobs"),
    import("../src/lib/companies"),
    import("../src/lib/preferences"),
    import("../src/components/DeleteAccount"),
  ]);
  return {
    sql: db.sql,
    currentUserId: currentUser.currentUserId,
    matchingJobsForCompany: jobs.matchingJobsForCompany,
    openJobsWithMatchFlag: jobs.openJobsWithMatchFlag,
    listCompanyCards: companies.listCompanyCards,
    loadPreferences: preferences.loadPreferences,
    DeleteAccount: deleteAccount.DeleteAccount,
  };
}

/** Set once loadLib() has run, so the finally below can close a pool that exists. */
let lib: Lib | undefined;

const BASE = process.env.UI_CHECK_BASE_URL ?? "http://localhost:3000";

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

/** Every protected surface, asked WITHOUT a session. */
async function gateIsShut() {
  console.log(`\nthe gate, against ${BASE} with no cookie at all`);

  const routes: [string, RequestInit][] = [
    ["/api/companies", {}],
    ["/api/preferences", {}],
    ["/api/companies/1/jobs?filter=matches", {}],
    [
      "/api/jobs/applied",
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ canonical_id: "nope", applied: true }),
      },
    ],
    [
      "/api/companies",
      { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" },
    ],
  ];

  for (const [path, init] of routes) {
    const res = await fetch(`${BASE}${path}`, { ...init, redirect: "manual" });
    const method = init.method ?? "GET";
    check(res.status === 401, `${method} ${path} is 401 unauthenticated`, `got ${res.status}`);
  }

  // Pages redirect rather than 401 — a browser asking for HTML should be sent
  // somewhere it can sign in. /add and /preferences are in this list because
  // until 2a neither called requirePage() at all and both leaned on the matcher.
  for (const path of ["/", "/jobs", "/preferences", "/add"]) {
    const res = await fetch(`${BASE}${path}`, { redirect: "manual" });
    const location = res.headers.get("location") ?? "";
    check(
      res.status >= 300 && res.status < 400 && location.includes("/signin"),
      `GET ${path} redirects to /signin unauthenticated`,
      `got ${res.status} -> ${location || "(no location)"}`,
    );
  }

  // The two holes in the matcher, which must stay open or nobody can ever sign
  // in. A 500 here is as much a failure as a 401 would be.
  const signin = await fetch(`${BASE}/signin`, { redirect: "manual" });
  check(signin.status === 200, "GET /signin is reachable signed out", `got ${signin.status}`);

  const callback = await fetch(`${BASE}/auth/callback`, { redirect: "manual" });
  check(
    callback.status >= 300 && callback.status < 400,
    "GET /auth/callback is reachable signed out (redirects, does not 401)",
    `got ${callback.status}`,
  );

  // The other half of the flow, and the one that regressed: sign-in STARTS at a
  // Route Handler now, because a Server Action's redirect() could not deliver
  // the browser to an external provider. Gated by the matcher it would redirect
  // to /signin, which points back here — a loop that reads as "the button does
  // nothing". So this asserts both that it is reachable AND that it actually
  // aims at the provider, since a redirect back to /signin would satisfy a
  // status-only check while being exactly the bug.
  const start = await fetch(`${BASE}/auth/signin?next=%2F`, { redirect: "manual" });
  const target = start.headers.get("location") ?? "";
  check(
    start.status >= 300 && start.status < 400 && target.includes("/auth/v1/authorize"),
    "GET /auth/signin redirects to Supabase's authorize endpoint signed out",
    `got ${start.status} -> ${target || "(no location)"}`,
  );
  check(
    (start.headers.get("set-cookie") ?? "").includes("code-verifier"),
    "and sets the PKCE code verifier on that same response",
    `set-cookie: ${start.headers.get("set-cookie") ?? "(none)"}`,
  );

  // The old Auth.js endpoints are gone. If this ever answers again, something
  // reinstated next-auth alongside Supabase and there are two session systems.
  const oldAuth = await fetch(`${BASE}/api/auth/session`, { redirect: "manual" });
  check(
    oldAuth.status === 404 || oldAuth.status === 401,
    "the retired /api/auth/* endpoints no longer serve a session",
    `got ${oldAuth.status}`,
  );
}

async function main() {
  await gateIsShut();

  // --- live rows, through the same functions the server components call ----
  //
  // currentUserId() is the retained MY_EMAIL lookup, and this is exactly what
  // it is retained FOR: a headless script with no request and no session that
  // still has to name a user in order to check anything about one. It lives in
  // scripts/ since 2b, so the request path cannot reach it even by accident; a
  // page or route resolves its user from the session — see lib/guard.ts.
  lib = await loadLib();
  const { currentUserId, loadPreferences, listCompanyCards, matchingJobsForCompany, openJobsWithMatchFlag } = lib;

  const userId = await currentUserId();
  console.log(`\nlive rows for the operator's account: ${userId}`);

  const prefs = await loadPreferences(userId);
  const companies = await listCompanyCards(userId);

  let home: JobView[] = [];
  let all: JobView[] = [];
  for (const company of companies) {
    const matches = await matchingJobsForCompany(company.id, prefs, userId);
    if (matches.length < 2) continue;
    home = matches;
    all = await openJobsWithMatchFlag(company.id, prefs, userId);
    break;
  }
  if (!home.length) {
    throw new Error("no company on the live database has 2+ matching jobs to render");
  }

  // --- HOME: the checkbox -------------------------------------------------
  console.log("\nHome — the Applied checkbox");
  const unticked = render({ ...home[0], applied: false, applied_at: null }, "matches");
  check(unticked.includes('type="checkbox"'), "an unticked row renders a checkbox");
  check(unticked.includes("Applied"), "the checkbox is labelled Applied");
  check(!unticked.includes('class="job applied"'), "an unapplied row is not dimmed");

  const stamp = "2026-07-27T09:30:00.000Z";
  const ticked = render({ ...home[0], applied: true, applied_at: stamp }, "matches");
  check(ticked.includes('class="job applied"'), "a ticked row carries the applied class (dim + strikethrough)");
  check(ticked.includes('checked=""') || ticked.includes("checked"), "the box renders checked");
  check(ticked.includes(`href="${home[0].url}"`), "the apply link survives being applied to");
  check(/title="Applied on [^"]+\. Untick to undo\."/.test(ticked), "the tooltip names the applied date");

  // --- HOME: the badges ---------------------------------------------------
  //
  // Since Phase 1f the input is `user_notified` — derived per user from
  // user_job_notifications by lib/jobs.notifiedToUser. Since 2a the user it is
  // derived FOR comes from the session on a real request; here it is the
  // operator's id, threaded explicitly, which is the same code path with the
  // same parameter.
  console.log("\nHome — NEW / NOTIFIED");
  const notified = render({ ...home[0], user_notified: true }, "matches");
  check(notified.includes('class="badge notified"'), "a match already emailed to me reads NOTIFIED");
  check(notified.includes(">NOTIFIED<"), "the word NOTIFIED is in the markup");
  check(!notified.includes(">NEW<"), "and NEW is not");

  const fresh = render({ ...home[0], user_notified: false }, "matches");
  check(fresh.includes('class="badge new"'), "a match not yet emailed to me reads NEW");
  check(fresh.includes(">NEW<"), "the word NEW is in the markup");
  check(!fresh.includes(">NOTIFIED<"), "and NOTIFIED is not");

  // The same row with NOTHING overridden — the badge the app actually puts on
  // screen, from the flag the live query produced. The two checks above prove
  // the component can render either badge; this proves the live per-user data
  // reaches it, which is the half a synthetic prop can never test. It is also
  // what would catch the query quietly dropping the field: `undefined` is
  // falsy, so every row would read NEW.
  console.log("\nHome — the badge on a LIVE row, as the query produced it");
  const liveRow = home[0];
  check(
    typeof liveRow.user_notified === "boolean",
    "the live query produces user_notified as a boolean",
    `got ${typeof liveRow.user_notified}`,
  );
  const live = render(liveRow, "matches");
  const expected = liveRow.user_notified ? "NOTIFIED" : "NEW";
  check(
    live.includes(`>${expected}<`),
    `the live row reads ${expected}, which is what user_job_notifications says for me — "${liveRow.title}"`,
  );

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
    check(
      typeof nonMatching.user_notified === "boolean",
      "and the raw list carries user_notified too, so that text is per-user as well",
    );
  }

  // --- PREFERENCES: the delete-account surface ----------------------------
  //
  // Phase 2c, and it is here for the reason this whole script exists: a control
  // that is specified, agreed to and never rendered fails silently, and "the
  // account can be deleted" is not a claim a type checker or a database query
  // can settle. Rendered with synthetic props — no account is created and
  // nothing is deleted; what is under test is the MARKUP, and the behaviour
  // behind it is proved against real rows by `npm run delete-check`.
  console.log("\nPreferences — the delete-account form");
  const account = renderToStaticMarkup(
    createElement(lib.DeleteAccount, {
      email: "someone@example.test",
      footprint: { companies: 50, notifications: 7708, hasResume: true },
    }),
  );
  check(account.includes("<details"), "the destructive control is behind a disclosure");
  check(account.includes('name="confirm"'), "and behind a typed confirmation field");
  check(account.includes("required"), "which the browser will not let you skip");
  check(account.includes("someone@example.test"), "the form names the address you must type");
  check(account.includes("50") && account.includes("7,708"), "and says what is about to be lost");
  // Slice 1: the resume is personal data and the confirmation has to name it,
  // for the same reason the counts are there — informed consent to erasure means
  // knowing what goes. Only when there is one: an account with no resume must
  // not be told it is losing a file it never uploaded.
  check(
    account.includes("resume"),
    "an account WITH a resume is told the resume goes too",
  );
  const noResume = renderToStaticMarkup(
    createElement(lib.DeleteAccount, {
      email: "someone@example.test",
      footprint: { companies: 3, notifications: 9, hasResume: false },
    }),
  );
  check(
    !noResume.includes("resume"),
    "an account WITHOUT one is not told it is losing a file it never uploaded",
  );
  check(
    account.includes("<form") && account.includes('type="submit"'),
    "it is a form with a submit button, not a button with a handler",
  );
  // WHAT THIS SECTION DELIBERATELY DOES NOT ASSERT, because it cannot see it.
  // The markup above carries `action="javascript:throw ..."` and a trailing
  // <script>: that is React's form-replay fallback, emitted because
  // renderToStaticMarkup runs OUTSIDE Next's bundler and so has no way to
  // serialise a server action reference. Next's own render emits a POST to the
  // page with the action id instead, and that is the output the
  // "works with JavaScript disabled" claim is about — so asserting "no <script>"
  // here would be asserting something about this harness rather than about the
  // page, and it would fail forever while the page was perfectly fine. The
  // half that IS checkable statically — no "use client", no onClick, no
  // window.confirm in the component — is asserted in
  // tests/test_account_deletion.py.
  const mismatch = renderToStaticMarkup(
    createElement(lib.DeleteAccount, {
      email: "someone@example.test",
      footprint: { companies: 0, notifications: 0, hasResume: false },
      notice: "mismatch",
    }),
  );
  check(
    mismatch.includes("did not match") && mismatch.includes("nothing was deleted"),
    "a failed confirmation says so, and says nothing was deleted",
  );

  // The Slice 1 refusal. It must lead with the fact that NOTHING was deleted:
  // an error on a delete screen reads as "partly done" unless it says otherwise,
  // and the whole point of the file-first ordering is that there is no partly.
  const resumeStuck = renderToStaticMarkup(
    createElement(lib.DeleteAccount, {
      email: "someone@example.test",
      footprint: { companies: 1, notifications: 1, hasResume: true },
      notice: "resume",
    }),
  );
  check(
    resumeStuck.includes("Nothing was deleted"),
    "a failed resume erasure says NOTHING was deleted, first",
  );

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
      ? `\nFAILED — ${failures} assertion(s) about the UI or the gate did not hold.`
      : `\nOK — every assertion held: the gate is shut at ${BASE}, and the shipped ` +
        `component renders the checkbox and the badges over live rows.`,
  );
  if (failures) process.exitCode = 1;
}

main()
  .catch((err) => {
    console.error("\nFAILED:", err instanceof Error ? err.message : err);
    console.error("Is the dev server running? `npm run dev` in another terminal.");
    process.exitCode = 1;
  })
  .finally(() => lib?.sql.end());
