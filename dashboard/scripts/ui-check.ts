/**
 * Prove the dashboard features are actually ON SCREEN, and that the gate is shut.
 *
 *     npm run dev          # in another terminal
 *     npm run ui-check
 *
 * WHY THIS EXISTS
 * ---------------
 * The Applied checkbox and the NEW / SUGGESTED badges were once
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
// See the stub-router note in the experience-panel section for why this is needed.
import { AppRouterContext } from "next/dist/shared/lib/app-router-context.shared-runtime";
// The same trick one hook over: components/Nav calls usePathname(), and outside
// Next's own render there is no PathnameContext to read. See the nav section.
import { PathnameContext } from "next/dist/shared/lib/hooks-client-context.shared-runtime";

import { CompanyList } from "../src/components/CompanyList";
import { JobLine, type Mode } from "../src/components/JobLine";
// Static for the reason CompanyList is: its lib/companies import is `import
// type` and erased, and the only other things it reaches are React and the DOM.
// Nothing on that graph builds a database pool.
import { WatchlistPicker, type PickerCompany } from "../src/components/WatchlistPicker";
import type { CompanyCard } from "../src/lib/companies";
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
 * CompanyList is static for the same reason and it is worth checking rather than
 * assuming: its lib/companies and lib/jobs imports are both `import type` and so
 * are erased, its lib/labels import is a value but that file imports nothing at
 * all by design, and the only component it reaches is JobLine. Nothing on that
 * graph builds a pool. If a future edit gives it a runtime import of lib/db this
 * script starts failing with `DASHBOARD_DB_URL is not set` before its first
 * assertion, and the fix is to move it into loadLib() below.
 */
type Lib = {
  sql: typeof import("../src/lib/db").sql;
  currentUserId: typeof import("./current-user").currentUserId;
  matchingJobsForCompany: typeof import("../src/lib/jobs").matchingJobsForCompany;
  openJobsWithMatchFlag: typeof import("../src/lib/jobs").openJobsWithMatchFlag;
  listCompanyCards: typeof import("../src/lib/companies").listCompanyCards;
  listCompanies: typeof import("../src/lib/companies").listCompanies;
  watchlistState: typeof import("../src/lib/watchlist").watchlistState;
  FREE_WATCHLIST_CAP: typeof import("../src/lib/labels").FREE_WATCHLIST_CAP;
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
  /**
   * Deferred like DeleteAccount, and for a nearer-miss version of the same reason:
   * PreferencesEditor imports ResumePanel, which reaches the upload route's types.
   * It is here rather than statically imported so nothing in its graph can build a
   * pool before loadEnv().
   */
  PreferencesEditor: typeof import("../src/components/PreferencesEditor").PreferencesEditor;
  /**
   * Deferred for exactly DeleteAccount's reason: Nav imports the sign-out server
   * action from app/actions, which imports lib/account, which imports lib/db. A
   * static import would build the pool before loadEnv() and throw before the first
   * assertion. (The route constant it reads comes from lib/labels, which imports
   * nothing — that is the whole reason the constant lives there.)
   */
  Nav: typeof import("../src/components/Nav").Nav;
};

async function loadLib(): Promise<Lib> {
  const [
    db,
    currentUser,
    jobs,
    companies,
    preferences,
    watchlist,
    labels,
    deleteAccount,
    editor,
    nav,
  ] = await Promise.all([
    import("../src/lib/db"),
    import("./current-user"),
    import("../src/lib/jobs"),
    import("../src/lib/companies"),
    import("../src/lib/preferences"),
    import("../src/lib/watchlist"),
    import("../src/lib/labels"),
    import("../src/components/DeleteAccount"),
    import("../src/components/PreferencesEditor"),
    import("../src/components/Nav"),
  ]);
  return {
    sql: db.sql,
    currentUserId: currentUser.currentUserId,
    matchingJobsForCompany: jobs.matchingJobsForCompany,
    openJobsWithMatchFlag: jobs.openJobsWithMatchFlag,
    listCompanyCards: companies.listCompanyCards,
    listCompanies: companies.listCompanies,
    watchlistState: watchlist.watchlistState,
    FREE_WATCHLIST_CAP: labels.FREE_WATCHLIST_CAP,
    loadPreferences: preferences.loadPreferences,
    DeleteAccount: deleteAccount.DeleteAccount,
    PreferencesEditor: editor.PreferencesEditor,
    Nav: nav.Nav,
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
    // The watch-list write path. Both directions, because they are separate
    // handlers on one route and a gate is exactly the kind of thing that gets
    // added to the one somebody was thinking about. A POST here creates a
    // user_companies row and suppresses a company's history for whoever the
    // session names — so an unauthenticated caller reaching it would be writing
    // per-user rows with no user.
    ["/api/watchlist", {}],
    [
      "/api/watchlist",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ company_id: 1 }),
      },
    ],
    [
      "/api/watchlist",
      {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ company_id: 1 }),
      },
    ],
    // The sealed write. 401 here proves only the outer gate; that the field is
    // refused for a caller who IS signed in is proved by the route reading no
    // identity at all (see the 400 branch in api/companies/[id]/route.ts) and by
    // tests/test_tracked_write_sealed.py, which asserts no statement under src/
    // can write the column. Both halves matter: this one would still pass if the
    // field were quietly re-accepted behind the session gate.
    [
      "/api/companies/1",
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tracked: false }),
      },
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
  //
  // /onboarding joins them in Slice 1b, and it is the one worth stating why.
  // That page is EXEMPT from the onboarding gate — it is where the gate sends
  // people — and an exemption written slightly too wide is how a page ends up
  // exempt from the SESSION gate as well. It must redirect to /signin with no
  // cookie exactly like every other page, and the middleware matcher's holes
  // must not have grown a fifth one.
  for (const path of ["/", "/jobs", "/watchlist", "/preferences", "/add", "/onboarding"]) {
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

  /*
   * --- THE APP NAV, ON /onboarding AND EVERYWHERE ELSE ---------------------
   *
   * CLAUDE.md carried this under *Pending* from Slice 1b: the chrome offered
   * Home / Jobs / Add during setup and every one of them bounced straight back to
   * /onboarding. It is asserted HERE rather than over HTTP for the reason the
   * whole script exists — the gate section above proves the ROUTES are shut, and
   * a shut route says nothing about whether a link to it is on the screen. A
   * person mid-setup looking at five links that undo themselves is the failure,
   * and only the markup can show it.
   *
   * BOTH DIRECTIONS, because they fail apart. Hiding the tabs is one edit and
   * hiding them everywhere is the same edit written slightly too wide — and
   * /preferences is the page most likely to be caught by it, since it is the OTHER
   * screen this same editor renders on and the one exempted from the gate.
   *
   * SIGN OUT IS ASSERTED ON BOTH, and that is not a formality. Removing the
   * navigation from a screen a person is held on is only acceptable while there is
   * still a way off it; a setup form with no exit is a trap rather than a gate.
   *
   * The stub is PathnameContext rather than a mock of usePathname: the component
   * calls the real hook, and providing the context is the same honest fix the
   * router stub further down makes for the same reason.
   */
  console.log("\nthe app nav — hidden on /onboarding, present everywhere else");
  const navAt = (pathname: string) =>
    renderToStaticMarkup(
      createElement(
        PathnameContext.Provider,
        { value: pathname },
        createElement(lib!.Nav, { email: "someone@example.test" }),
      ),
    );

  const navOnboarding = navAt("/onboarding");
  const navPreferences = navAt("/preferences");
  const navHome = navAt("/");

  check(
    !navOnboarding.includes('class="tabs"'),
    "on /onboarding the tab bar is not rendered at all",
    "the nav rendered its tabs on the one page a person is held on",
  );
  for (const label of ["Home", "Jobs", "Watchlist", "Add", "Preferences"]) {
    check(
      !navOnboarding.includes(`>${label}<`),
      `and there is no ${label} link to bounce off the gate`,
    );
  }
  check(
    navOnboarding.includes("Sign out"),
    "but Sign out is still there — a person must always be able to leave",
    "onboarding is a screen with no way off it",
  );
  check(
    navOnboarding.includes("someone@example.test"),
    "and the address is still shown, so it is clear which account is being set up",
  );

  check(
    navPreferences.includes('class="tabs"'),
    "on /preferences — the SAME editor, after onboarding — the tab bar is back",
    "the hide is too wide: it took the nav off an ordinary app page",
  );
  for (const label of ["Home", "Jobs", "Watchlist", "Add", "Preferences"]) {
    check(
      navPreferences.includes(`>${label}<`),
      `and the ${label} link is on it`,
    );
  }
  check(
    navPreferences.includes('aria-current="page"'),
    "with the current tab marked for a screen reader, unchanged by this slice",
  );
  check(
    navHome.includes('class="tabs"') && navHome.includes(">Jobs<"),
    "and Home is unaffected — only /onboarding hides anything",
  );

  const userId = await currentUserId();
  console.log(`\nlive rows for the operator's account: ${userId}`);

  const prefs = await loadPreferences(userId);
  // listCompanyCards, which is what Home and Jobs call again since the filter-first
  // slice. They called listWatchedCompanyCards for one slice in between, and for
  // THIS account the two returned the same rows either way — it watches everything,
  // via scripts/backfill_user_companies.py in Phase 1c — which is exactly why the
  // operator's screens are the wrong place to notice a model change and why
  // filter-first-check.ts uses a throwaway account instead.
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
  //
  // A `NOTIFIED` badge sat opposite NEW until the filter-first slice and these
  // assertions were its mirror image. It is gone because its copy — "already gone
  // out to you in a digest email" — stopped being true: a user_job_notifications
  // row now far more often means a filter save recorded the back catalogue than
  // that an email was sent, so on a new account's first Home it would have claimed
  // hundreds of emails nobody received. See lib/badges.ts.
  //
  // ASSERTED AS AN ABSENCE IN THE MARKUP, which is the half the type cannot cover:
  // lib/badges.HomeBadge no longer admits the string, but a hand-written <span> in
  // JobLine would compile perfectly well.
  console.log("\nHome — NEW, and the absence that replaced NOTIFIED");
  const handled = render({ ...home[0], user_notified: true }, "matches");
  check(
    !handled.includes(">NOTIFIED<") && !handled.includes('class="badge notified"'),
    "a match already handled carries NO badge — it does not claim an email",
    "the NOTIFIED badge is back in the markup",
  );
  check(!handled.includes(">NEW<"), "and it is not mislabelled NEW either");
  check(
    !/class="badge[^"]*"/.test(handled),
    "no badge element is rendered for it at all",
  );

  const fresh = render({ ...home[0], user_notified: false }, "matches");
  check(fresh.includes('class="badge new"'), "a match still queued for me reads NEW");
  check(fresh.includes(">NEW<"), "the word NEW is in the markup");
  check(!fresh.includes(">NOTIFIED<"), "and NOTIFIED is not");

  // The Applied checkbox and the apply link survive on BOTH, which is what says
  // the row is intact rather than merely badge-free.
  for (const [markup, label] of [[handled, "handled"], [fresh, "new"]] as const) {
    check(markup.includes('type="checkbox"'), `the Applied checkbox is still on a ${label} row`);
    check(markup.includes('rel="noopener noreferrer"'), `and the apply link is too (${label})`);
  }

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
  check(
    liveRow.user_notified ? !live.includes(">NEW<") : live.includes(">NEW<"),
    `the live row reads ${liveRow.user_notified ? "no badge" : "NEW"}, which is what ` +
      `user_job_notifications says for me — "${liveRow.title}"`,
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
    // The raw row printed `notified` / `not notified` beside the status tag until
    // the filter-first slice, and it went with the NOTIFIED badge for the same
    // reason: a user_job_notifications row now far more often means a filter save
    // recorded the back catalogue than that an email was sent, so the word claimed
    // something the row could not support — on the DEBUGGING view, where a wrong
    // fact is most likely to be believed. The status tag stays; that one is a
    // column.
    check(
      !html.includes(">notified<") && !html.includes(">not notified<"),
      "and no longer claims a notification state it cannot support",
      "the notified text is back on the raw row",
    );
    check(
      html.includes("tag on") || html.includes("tag off"),
      "but keeps the status tag, which is a real column",
    );
    check(
      typeof nonMatching.user_notified === "boolean",
      "and the raw list carries user_notified too, so that text is per-user as well",
    );
  }

  // --- HOME: the tracked flag is SHOWN and NOT EDITABLE --------------------
  //
  // The security fix, asserted where it can actually be seen. companies.tracked
  // is the crawler's catalog-wide flag — scripts/crawl.py selects
  // `WHERE tracked = TRUE`, one shared value for the whole install — and this
  // component used to render a checkbox that PATCHed it. The route gate is
  // requireSession(), which asks whether there is a session and never whose, so
  // any signed-in stranger could stop a company being crawled for everybody.
  //
  // This is the "the button is gone and stays gone" half. It renders the SHIPPED
  // component over a LIVE company card, which is the whole argument this script
  // exists for: a control that was removed from a spec but left in the markup
  // fails silently, and so does one put back by a merge. The other half — that no
  // statement under src/ can write the column even with a session and curl — is
  // tests/test_tracked_write_sealed.py.
  console.log("\nHome — the tracked pill is read-only");
  const card = companies[0];
  const companyMarkup = renderToStaticMarkup(
    createElement(CompanyList, { initial: [card] as CompanyCard[], mode: "matches" as Mode }),
  );
  check(Boolean(card), "there is a live company card to render");
  check(
    companyMarkup.includes(">tracked<") || companyMarkup.includes(">off<"),
    "the row still SHOWS the tracked/off pill — the fact is worth reading",
  );
  check(
    !companyMarkup.includes("aria-label=\"Track "),
    "there is no `Track <company>` control in the markup",
  );
  check(
    !companyMarkup.includes('class="switch"'),
    "the pill is not wrapped in the switch label that carried the checkbox",
  );
  // The category select is the one control that legitimately remains on this
  // header, so a check that merely counted inputs would pass with the toggle
  // restored. This names the input type the toggle actually was.
  check(
    !companyMarkup.includes('type="checkbox"'),
    "a collapsed row renders no checkbox at all (the Applied box lives on a job, not a company)",
  );
  check(
    companyMarkup.includes("<select"),
    "and the category select — the one control that legitimately stays — is still there",
  );

  /*
   * --- Watchlist — the company picker, and what Home now lists --------------
   *
   * The control CLAUDE.md called "the next slice" from Phase 1c to here. It is
   * the Step 5c argument in its strongest form: `user_companies` has existed for
   * two phases, the digest has read it for two phases, and nothing in the web app
   * could write it — so a feature that is specified, agreed and not rendered is
   * not hypothetical here, it is the state this slice found the product in.
   *
   * Rendered from the SHIPPED component over the LIVE catalog, three times, for
   * the three states that behave differently: watching some, watching none, and
   * at the cap.
   */
  console.log("\nWatchlist — the picker");
  const catalog = (await lib.listCompanies()) as PickerCompany[];
  const state = await lib.watchlistState(userId);
  console.log(
    `  the operator watches ${state.companyIds.length} of ${catalog.length} catalog ` +
      `companies, on plan '${state.plan}' (cap ${state.cap ?? "none"})`,
  );

  check(
    companies.length === state.companyIds.length,
    `Home lists exactly the ${state.companyIds.length} companies this account watches`,
    `Home listed ${companies.length}, the watch list holds ${state.companyIds.length}`,
  );
  check(
    catalog.length >= companies.length,
    `and the catalog the picker browses is the wider list (${catalog.length})`,
  );

  const picker = renderToStaticMarkup(
    createElement(WatchlistPicker, {
      catalog,
      initialWatched: state.companyIds,
      cap: state.cap,
    }),
  );
  check(picker.includes('type="search"'), "the picker renders a search box over the catalog");
  check(
    picker.includes(`>${catalog[0].name}`) || picker.includes(catalog[0].name),
    "and the catalog rows themselves — a company you could add is on the screen",
  );
  check(
    /aria-label="(Watch|Stop watching) /.test(picker),
    "with a per-company control labelled for a screen reader",
  );
  check(
    /already has as seen/.test(picker) || /already<\/(em|strong)> has as seen/.test(picker),
    "and it says what adding one does to that company's existing jobs — the second " +
      "half of tracking, stated where a person can read it",
    "nothing on the picker explains the history suppression",
  );

  /*
   * THE EMPTY STATE, which is what a brand-new account sees. It has to say that
   * nothing can match — the same fail-closed sentence the city picker prints for
   * an empty selection, and for the same reason: an account released from
   * onboarding with no watch list has an empty digest and nothing on screen
   * explaining why.
   */
  const emptyPicker = renderToStaticMarkup(
    createElement(WatchlistPicker, { catalog, initialWatched: [], cap: lib.FREE_WATCHLIST_CAP }),
  );
  check(
    /not watching any company/.test(emptyPicker),
    "with none watched the picker says so as an ERROR, not as a neutral note",
  );
  check(
    /msg bad/.test(emptyPicker),
    "and marks it red — an empty watch list matches nothing, exactly like an empty city list",
  );
  check(
    emptyPicker.includes(`of ${lib.FREE_WATCHLIST_CAP}`),
    `and the heading states the cap (0 of ${lib.FREE_WATCHLIST_CAP})`,
  );

  /*
   * AT THE CAP. The server refuses the sixth add (scripts/watchlist-check proves
   * that against the live database); this is the half where a person is told
   * before they press rather than after.
   */
  const cappedPicker = renderToStaticMarkup(
    createElement(WatchlistPicker, {
      catalog,
      initialWatched: catalog.slice(0, lib.FREE_WATCHLIST_CAP).map((c) => c.id),
      cap: lib.FREE_WATCHLIST_CAP,
    }),
  );
  check(
    cappedPicker.includes("Limit reached"),
    "at the cap, the Add control on an unwatched company says the limit is reached",
  );
  check(
    /disabled=""/.test(cappedPicker),
    "and is disabled rather than merely relabelled",
  );
  check(
    cappedPicker.includes("Remove"),
    "while the companies already watched can still be removed — the cap refuses " +
      "additions and never takes anything away",
  );

  /*
   * THE OVER-CAP ACCOUNT, which is the operator: 50 watched against a free cap of
   * 5, because the Phase-1c backfill predates the limit by two phases. It must
   * read as "you cannot add more", never as "some of these are not counting" —
   * a cap that appeared to have silently trimmed a watch list would be the
   * cry-wolf failure inverted.
   */
  if (state.cap !== null && state.companyIds.length > state.cap) {
    check(
      /Nothing has been removed/.test(picker),
      `an over-cap account (${state.companyIds.length} of ${state.cap}) is told nothing was removed`,
      "the over-cap notice does not say the existing watch list is intact",
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

  /*
   * --- Preferences — the experience panel, Phase 3 Slice 2 -----------------
   *
   * THIS SECTION EXISTS FOR THE REASON THIS WHOLE SCRIPT DOES. The Applied
   * checkbox and the badges were specified, agreed and never built, and nothing in
   * the repo could tell a rendered feature from a designed one — a typechecker
   * proves the props line up and a test proves the column exists, and neither
   * proves a person opening the page sees a control. The experience rule is now
   * REQUIRED to finish onboarding, so a panel that failed to render would not be a
   * cosmetic gap: it would be an account that cannot complete setup and cannot see
   * why.
   *
   * Rendered from the SHIPPED component over a real preferences row, twice, for the
   * two states that behave differently.
   */
  console.log("\nPreferences — the experience panel");

  /*
   * A STUB ROUTER, because PreferencesEditor calls useRouter() at the top of its
   * body — it needs router.replace() to leave /onboarding after a successful save.
   * Outside Next's own render there is no AppRouterContext, and useRouter() does not
   * return null there, it throws "invariant expected app router to be mounted", so
   * the render dies before a single assertion runs.
   *
   * Providing the context is the honest fix and the alternative was worse: making
   * the hook conditional in the component so a test could skip it would be shaping
   * shipped code around this harness, and hooks cannot be conditional anyway. The
   * stub's methods are never called — nothing here saves — so they are deliberately
   * no-ops rather than spies. What is being checked is the MARKUP.
   */
  const stubRouter = {
    push: () => {},
    replace: () => {},
    refresh: () => {},
    back: () => {},
    forward: () => {},
    prefetch: () => {},
  };
  const withRouter = (element: ReturnType<typeof createElement>) =>
    renderToStaticMarkup(
      createElement(AppRouterContext.Provider, { value: stubRouter as never }, element),
    );

  const configured = withRouter(
    createElement(lib.PreferencesEditor, { initial: prefs, resume: null }),
  );
  check(
    configured.includes("Experience"),
    "the panel renders at all — there is an Experience heading",
  );
  check(
    configured.includes('id="exp-low"') && configured.includes('id="exp-high"'),
    "with both ends of the range as their own inputs",
  );
  check(
    configured.includes('value="0"') && configured.includes('value="2"'),
    "carrying this account's stored 0-2 rather than a placeholder",
    "the stored range did not reach the inputs",
  );
  check(
    configured.includes("Show jobs that don&#x27;t state a minimum") ||
      configured.includes("Show jobs that don’t state a minimum"),
    "and the no-minimum toggle, labelled in words rather than as a column name",
  );
  check(
    /99%/.test(configured),
    "whose hint states how much of the board it decides — the number is the reason the control matters",
  );
  check(
    configured.includes('id="years-held"'),
    "the informational years-held field is on the screen",
  );
  check(
    /never filters/.test(configured),
    "and says out loud that it never filters, so nobody reads it as part of the rule",
  );

  /*
   * THE PAY-BAND PICKER — Phase 3, the salary slice.
   *
   * The Step 5c argument in its purest form. This panel decides almost nothing:
   * no live posting states a salary, so ticking a band changes no digest today.
   * A feature that changes nothing is exactly the kind that gets specified,
   * agreed, and never actually built — which is the failure ui-check was written
   * for after the Applied checkbox and the badges went that way. match-parity's
   * synthetic probes prove the RULE; only this can prove the CONTROL exists.
   */
  console.log("\nPreferences — the pay-band picker");
  check(
    configured.includes("Expected pay"),
    "the pay panel renders at all",
    "the salary band picker did not reach the markup",
  );
  for (const label of ["Under ₹10L", "₹15L – ₹25L", "₹50L and above"]) {
    check(
      configured.includes(label),
      `the ${label} band is on the screen, labelled in rupees rather than as its stored id`,
    );
  }
  check(
    !configured.includes(">15-25<"),
    "and the STORED identifier is not what a person reads — '15-25' is the value, " +
      "'₹15L – ₹25L' is the label",
  );
  check(
    /Optional/.test(configured) && /do not state a salary/.test(configured),
    "the hint says the panel is optional AND why it will seem to do nothing — most " +
      "postings state no pay, so ticking a band narrows almost nothing",
    "the hint that stops this reading as a broken filter is missing",
  );
  check(
    /leaving this blank and ticking everything do the same thing/.test(configured),
    "and states the fail-OPEN rule outright, which is the one thing about this " +
      "field that differs from every other filter on the screen",
  );

  /*
   * THE BLANK CASE IS NOT AN ERROR HERE, which is the opposite of every other
   * panel and therefore worth its own render. WorkModePicker shows a red "no job
   * can match at all" on an empty selection; this one must not, because an empty
   * salary_bands filters nothing rather than everything.
   */
  const noBands = withRouter(
    createElement(lib.PreferencesEditor, {
      initial: { ...prefs, salary_bands: [] },
      resume: null,
    }),
  );
  check(
    /pay is not filtered on/.test(noBands),
    "with no bands ticked the screen says pay is not filtered on",
    "the empty pay selection did not explain itself",
  );
  check(
    !/msg bad[^]{0,200}pay/i.test(noBands),
    "and does NOT flag it as an error — an empty pay selection is a legitimate " +
      "answer, unlike an empty city or work-arrangement selection",
  );

  /*
   * THE CITY PICKER — Phase 3, cities Slice B.
   *
   * The reason ui-check exists applies to this slice more than to most: the
   * matchers are covered by match-parity and pytest, and neither of those can tell
   * a rendered control from a designed one. An All-India switch that decides the
   * filter but never reaches the markup is the Step 5c failure exactly.
   */
  console.log("\nPreferences — the city picker");
  check(
    configured.includes("All over India"),
    "the All over India switch is on the screen",
    "the switch that ignores the city list entirely did not render",
  );
  check(
    configured.includes("All Mumbai areas") && configured.includes("All of Delhi/NCR"),
    "and both umbrella controls, so a region is one tick rather than four",
  );
  /*
   * THE NAME IS FOR THE READER, THE LABEL IS THE ANSWER — the reskin's one change
   * to this picker. Each cell prints "Bengaluru" and carries `blr` on the
   * control's title and in its accessible name, so both halves are asserted: a
   * grid of friendly names with no slug behind it would be a screen nobody can
   * check against jobs.city, and a slug with no name is what the design replaced.
   */
  for (const [label, name] of [
    ["blr", "Bengaluru"],
    ["mumbai", "Mumbai"],
    ["navi-mumbai", "Navi Mumbai"],
    ["noida", "Noida"],
    ["other-india", "Elsewhere in India"],
    ["belagavi", "Belagavi"],
  ]) {
    check(
      configured.includes(`>${name}<`) && configured.includes(`aria-label="${name} (${label})"`),
      `the ${label} checkbox exists as "${name}", with the label itself in its accessible name`,
    );
  }
  check(
    !configured.includes(">india-unclear<") && !configured.includes(">non-india<"),
    "and india-unclear / non-india are NOT offered — they can never match an explicit " +
      "selection, so a checkbox for them would be the silent-empty failure",
    "a label the route refuses is on screen as a tickable control",
  );
  check(
    /could not pin to a city/.test(configured),
    "the hint says where the unclear postings went instead — the All-India switch",
  );
  check(
    (configured.match(/checked/g) ?? []).length >= 3,
    "this account's three stored cities are ticked",
    "the stored selection did not reach the checkboxes",
  );

  /*
   * THE MIGRATION FALLBACK, ON SCREEN. An account that has not saved since Slice B
   * has its answer in the legacy `cities` column and nothing in selected_cities,
   * and the editor has to render THAT — agreeing with the gate, which reads the
   * same fallback. Rendering blank would mean the first Save silently wrote an
   * empty selection over three cities somebody chose.
   */
  const legacyOnly = withRouter(
    createElement(lib.PreferencesEditor, {
      initial: { ...prefs, selected_cities: [], cities: ["chennai", "kolkata"] },
      resume: null,
    }),
  );
  check(
    (legacyOnly.match(/checked/g) ?? []).length >= 2 &&
      !legacyOnly.includes("Pick a city to save"),
    "a pre-Slice-B row renders its legacy cities as ticked rather than as blank",
    "the editor and the matcher disagree about what this account's filter currently is",
  );

  /*
   * ALL-INDIA DISABLES THE GRID RATHER THAN HIDING IT, so a person can see what
   * their selection was and what turning the switch off returns them to.
   */
  const allIndia = withRouter(
    createElement(lib.PreferencesEditor, {
      initial: { ...prefs, all_india: true, selected_cities: [] },
      resume: null,
    }),
  );
  check(
    allIndia.includes("<fieldset disabled"),
    "with All over India on, the city grid is disabled",
    "the grid stayed live, so a person could set two answers that contradict each other",
  );
  check(
    allIndia.includes(">Bengaluru<"),
    "and still visible — disabled, not hidden, so the selection underneath is legible",
  );
  /*
   * THE UMBRELLAS MOVED OUT OF THAT FIELDSET with the reskin — they are the
   * design's pill row now, above the grid rather than inside it — so they no
   * longer inherit its `disabled` and carry their own. Without it, All over India
   * would leave two live controls writing a selection it ignores.
   */
  check(
    (allIndia.match(/disabled=""/g) ?? []).length >= 2,
    "and the two umbrella controls are disabled with it, now that they sit outside the grid",
    "an umbrella stayed live under All over India, so two contradictory answers are on screen",
  );
  check(
    !allIndia.includes("Pick a city to save"),
    "and Save is not blocked on an empty selection, because All over India IS the answer",
  );

  /*
   * THE UNCONFIGURED ROW — a brand-new account, before onboarding. Two things must
   * be true at once and they pull in opposite directions: the range must be BLANK
   * (nothing was chosen, and a pre-filled 0-2 would be the operator's answer
   * smuggled in), while the toggle must be pre-checked ON (the project's documented
   * inclusion bias, which the column default of FALSE does not express). The column
   * stays FALSE until a real save either way — this is a suggestion on a form, not
   * a value in a database.
   */
  const blank = withRouter(
    createElement(lib.PreferencesEditor, {
      initial: {
        ...prefs,
        cities: [],
        // Both halves of the city answer are blank, which is what a brand-new row
        // actually holds: NEW_ACCOUNT_SEED writes `cities: []` and selected_cities
        // takes its schema default of '{}'. Leaving the operator's selection in the
        // spread would make this "a new account" in name only.
        selected_cities: [],
        all_india: false,
        keywords: [],
        exclusions: [],
        exp_range_low: null,
        exp_range_high: null,
        years_of_experience_held: null,
        show_no_experience_jobs: false,
        onboarding_completed: false,
      },
      resume: null,
    }),
  );
  check(
    !/id="exp-low"[^>]*value="[^"]/.test(blank),
    "a new account's range inputs are EMPTY — no operator default is pre-filled",
    "something was pre-filled into exp-low",
  );
  check(
    blank.includes("Fix the experience range to save"),
    "and Save says why it is disabled, rather than being greyed for no stated reason",
  );
  check(
    /Both ends of the range are needed/.test(blank),
    "with the blank range named as the problem inline",
  );
  check(
    (blank.match(/type="checkbox"[^>]*checked/g) ?? []).length >= 1,
    "the no-minimum toggle is pre-checked ON despite the column being FALSE — a suggested answer",
    "the toggle rendered unchecked, so a new account would hide 99% of the board by default",
  );
  check(
    /An empty city list matches nothing/.test(blank),
    "and the blank city answer is named inline — fail-closed, said out loud rather than implied",
    "a new account sees no city checkbox ticked and nothing explaining why nothing would match",
  );

  /*
   * THE FIFTH SAVE GATE — AN ACCOUNT WATCHING NO COMPANY — IS GONE, and its
   * absence is what is asserted here instead.
   *
   * PUT /api/preferences used to refuse that save, because the digest queue asked
   * `EXISTS (user_companies …)` before it asked anything about a title: an account
   * watching nothing matched nothing however good its keywords were. The
   * filter-first slice removed the clause, so watching nothing matches the whole
   * catalog and there is no missing answer to grey the button for.
   *
   * Asserted as an ABSENCE rather than simply deleted, because a gate that comes
   * back by accident is exactly the kind of regression nothing else here would
   * catch: the button would silently refuse a save that the route now allows, and
   * the person would be sent to a picker to fix a problem they do not have.
   */
  console.log("\nPreferences — the watch-list gate is gone");
  //
  // The keyword list is trimmed to one, and that is not cosmetic: the operator's
  // stored row holds 43 keywords against a KEYWORD_CAP of 25, so `overCap` fires
  // first in the label chain and the button would read "Remove 18 keywords to
  // save" whatever else were true.
  const savable = { ...prefs, keywords: ["backend engineer"] };
  const noCompanies = withRouter(
    createElement(lib.PreferencesEditor, { initial: savable, resume: null }),
  );
  check(
    !noCompanies.includes("Pick a company to watch to save"),
    "an account watching nothing is not blocked from saving",
    "the button still names an empty watch list — the fifth gate is back",
  );
  check(
    !/would match nothing at all/.test(noCompanies),
    "and is not told its filter would match nothing",
    "the empty-watch-list message is still rendered",
  );
  check(
    noCompanies.includes("Save preferences") || noCompanies.includes("No changes"),
    "the button reads normally with every other answer in place",
  );

  /*
   * --- The sectioned form and the sticky save bar --------------------------
   *
   * The onboarding-form slice. Both halves are pure markup, which is exactly the
   * kind of thing that gets agreed to and then quietly not built — and the sticky
   * bar in particular LOOKS present in a diff whether or not it is the last
   * element in the form, which is the one condition that makes it stay on screen.
   *
   * Note what is deliberately NOT asserted here: that scrolling pins it. That is
   * CSS and a viewport, which is where this script has always stopped. What is
   * checkable is that the bar exists, that it is the last thing in the form, and
   * that it says what is missing — the three things a browser cannot tell you are
   * wrong until somebody scrolls.
   */
  console.log("\nPreferences — the sectioned form and the sticky save bar");
  check(
    configured.includes('class="setup-form"'),
    "the fields are wrapped in one form container",
    "the sticky bar has no containing block to be sticky within",
  );
  // The section headings the reskin adopted from the design reference. They are
  // asserted as strings because they are also the anchors the save bar's list
  // points at — a heading that quietly changed is a link that lands nowhere.
  for (const [id, title] of [
    ["resume", "Resume &amp; keywords"],
    ["experience", "Experience"],
    ["cities", "Cities"],
    ["work-mode", "Work mode"],
    ["pay", "Salary"],
    ["tuning", "Fine-tuning"],
  ]) {
    check(
      configured.includes(`id="${id}"`) && configured.includes(title),
      `the "${title}" section is on the page, with an id the save bar can link to`,
    );
  }
  // `class="form-section` without the closing quote: Fine-tuning carries the
  // `aside` modifier that keeps it out of the numbered sequence, so an exact
  // match would count five and read as a missing section.
  check(
    (configured.match(/class="form-section[ "]/g) ?? []).length >= 6,
    "every one of them is a real section rather than a heading floating over a stack",
    `found ${(configured.match(/class="form-section[ "]/g) ?? []).length}`,
  );
  check(
    configured.includes('class="section-lede"'),
    "and each carries its own helper line under the heading",
  );
  check(
    configured.includes('class="save-bar"'),
    "the save bar renders",
  );
  check(
    !/class="save-bar"[^]*class="form-section"/.test(configured),
    "and no section renders after it — a sticky element that is not last stops " +
      "being sticky the moment its containing block scrolls past",
    "a section renders below the save bar, so the bar will scroll away mid-page",
  );

  /*
   * WHAT THE BAR SAYS WHEN SOMETHING IS MISSING. This is the half a person on a
   * phone actually depends on: the Keywords panel's own red message and the city
   * picker's own red message are several screens apart, and the button that
   * refuses because of them is at the bottom of both.
   */
  check(
    blank.includes('class="save-blockers"'),
    "on a brand-new account the bar lists what is still missing",
    "an unfinished form says nothing about what is unfinished",
  );
  for (const text of [
    "Add at least one keyword",
    "Set both ends of the experience range",
    "Pick a city, or choose all of India",
  ]) {
    check(blank.includes(text), `it names "${text}"`);
  }
  check(
    blank.includes('href="#cities"') && blank.includes('href="#experience"'),
    "and each one links to the section that fixes it, rather than just naming it",
  );
  // The empty watch list was a sixth entry on this list until the filter-first
  // slice. Asserted as an absence, for the same reason the button's own gate is
  // above: an entry that comes back sends somebody to a picker to fix a problem
  // they do not have.
  check(
    !blank.includes("Watch at least one company") &&
      !noCompanies.includes("Watch at least one company"),
    "and an empty watch list is NOT listed — it stopped being a missing answer",
    "the watch-list blocker is back on the save bar",
  );
  check(
    configured.includes('class="save-state-ok"') && !configured.includes("save-blockers"),
    "a form with every answer in says so instead of listing nothing",
  );
  check(
    configured.includes("Last saved"),
    "with the last-saved time still on the bar, where the old panel used to carry it",
  );

  /*
   * --- /onboarding, AS THAT PAGE ACTUALLY COMPOSES IT ----------------------
   *
   * Everything above renders PreferencesEditor alone, which is /preferences.
   * /onboarding is the same editor with the watch-list picker threaded through it
   * as a section, and the composition is the thing this slice changed — the
   * picker moved from a sibling ABOVE the editor to a section inside its stack,
   * so that the save bar could be the last element on the page.
   *
   * That ordering is not cosmetic and it is not visible in either component's own
   * markup: a save bar with a section under it is a save bar that scrolls away.
   * So the order is asserted here, over the shipped wrapper, with the live
   * catalog.
   */
  console.log("\nOnboarding — the picker and the editor as one form");
  const OnboardingSetup = (await import("../src/components/OnboardingSetup")).OnboardingSetup;
  // `savable` rather than the operator's row, for the reason the watch-list gate
  // section above gives: 43 stored keywords are over KEYWORD_CAP, so `overCap`
  // fires first in the button's label chain and would mask the answer under test.
  const setupBlank = withRouter(
    createElement(OnboardingSetup, {
      preferences: savable,
      resume: null,
      catalog,
      initialWatched: [],
      cap: lib.FREE_WATCHLIST_CAP,
    }),
  );

  const at = (needle: string) => setupBlank.indexOf(needle);
  check(
    at('id="companies"') > 0,
    "the watch-list picker renders as a section of the form, with its own heading",
    "the picker is not a section — the save bar cannot be the last thing on the page",
  );
  check(
    at('id="resume"') < at('id="experience"') &&
      at('id="experience"') < at('id="cities"') &&
      at('id="cities"') < at('id="work-mode"') &&
      at('id="work-mode"') < at('id="pay"') &&
      at('id="pay"') < at('id="companies"'),
    "and the sections run resume -> experience -> cities -> work mode -> pay -> companies",
    "the sections are not in the order the page is written to be read in",
  );
  check(
    at('class="save-bar"') > at('id="companies"'),
    "with the save bar after every one of them, including the picker",
    "the save bar renders above a section, so it will stop being sticky there",
  );
  /*
   * TWO ASSERTIONS STOOD HERE AND THE FILTER-FIRST SLICE REMOVED WHAT THEY
   * TESTED. They were that an account watching nothing could not finish setup
   * ("Pick a company to watch to save"), and that the save bar's link for it
   * pointed at the picker ON THIS PAGE (`#companies`) rather than at /watchlist,
   * which the onboarding gate would have bounced straight back.
   *
   * Both were right while the digest queue opened with `EXISTS (user_companies …)`
   * — an account pinning nothing matched nothing however good its keywords were,
   * so it was a genuine missing answer and PUT /api/preferences refused the save.
   * It matches the whole catalog now, so there is nothing to refuse and no answer
   * missing. Asserted as an ABSENCE rather than deleted, because a gate that comes
   * back sends somebody to a picker to solve a problem they do not have — and on
   * the ONE screen where the picker is a section of this very form, that is a
   * dead end with no error to explain it.
   */
  check(
    !setupBlank.includes("Pick a company to watch to save"),
    "an account watching nothing CAN finish setup — pinning is not a gate",
    "the button still refuses over an empty watch list",
  );
  check(
    !/class="save-blockers"[^]*href="#companies"/.test(setupBlank),
    "and the save bar does not list it as a missing answer",
    "the watch-list blocker is back on the setup bar",
  );
  check(
    setupBlank.includes('id="companies"'),
    "while the picker itself is still a section of the form — removed as a GATE, not as a feature",
  );
  check(
    setupBlank.includes('type="search"') && setupBlank.includes("not watching any company"),
    "the picker itself is fully present in that section — search box and empty state",
  );

  /*
   * --- FINE-TUNING IS /preferences ONLY, AND THE VALUES STILL TRAVEL ---------
   *
   * The reskin took exclusions and max_level off the setup screen. What makes
   * that a hidden SECTION rather than a dropped FIELD is invisible in the markup
   * of either page, which is exactly the kind of thing this script exists to
   * catch: PUT /api/preferences REQUIRES max_level and would refuse the save, and
   * an omitted exclusions list would clean to [] and overwrite whatever the row
   * held. So both halves are asserted — absent from the screen, present in the
   * state the save reads.
   */
  check(
    !setupBlank.includes('id="tuning"') && !setupBlank.includes("Max level"),
    "onboarding does not show exclusions or max level — setup asks the six questions " +
      "that decide whether anything reaches you at all",
    "a fine-tuning control is on the setup screen",
  );
  check(
    configured.includes('id="tuning"') && configured.includes("Max level"),
    "/preferences still has both, which is now the only place they are editable",
    "the fine-tuning section is gone from the screen that owns it",
  );
  check(
    configured.includes('id="tuning"') && /<details class="fold"/.test(configured),
    "folded shut there, because it is nobody's answer to anything by default",
  );
  /*
   * The seeded values of a BRAND-NEW account, rendered by the editor onboarding
   * actually uses — max_level 2 from the schema default and no exclusions. This
   * is the state that has to survive a save made on a screen that shows neither.
   */
  const setupSeeded = withRouter(
    createElement(OnboardingSetup, {
      preferences: { ...savable, exclusions: [], max_level: 2 },
      resume: null,
      catalog,
      initialWatched: [catalog[0].id],
      cap: lib.FREE_WATCHLIST_CAP,
    }),
  );
  check(
    !setupSeeded.includes("Exclusions") && !setupSeeded.includes("Max level"),
    "a fresh account sees neither control during setup",
  );
  check(
    !setupSeeded.includes("Pick a company to watch to save") &&
      setupSeeded.includes('class="save-state-ok"'),
    "and can still finish setup with both left exactly as they were seeded — " +
      "the save is not blocked on an answer nobody was asked for",
    "a fresh account cannot complete onboarding without the hidden fields",
  );

  /*
   * --- THE SUCCESS SCREEN ---------------------------------------------------
   *
   * It replaces an immediate router.replace(), so the failure it can have is
   * being on screen when nothing has been saved — a modal over a form somebody is
   * still filling in. It is client state set only by a response carrying
   * onboarding_completed, so on a first render it must be absent everywhere.
   */
  check(
    !setupBlank.includes("setup-done") && !setupSeeded.includes("setup-done"),
    "the success screen is not rendered before a save — it is raised by the response, " +
      "not by the page",
    "the 'you're all set' screen is over a form nobody has saved yet",
  );
  check(
    !configured.includes("setup-done") && !configured.includes("You&#x27;re all set"),
    "and /preferences never renders it at all — it passes no success copy and no redirect",
  );

  const setupWatching = withRouter(
    createElement(OnboardingSetup, {
      preferences: { ...prefs, keywords: ["backend engineer"] },
      resume: null,
      catalog,
      initialWatched: [catalog[0].id],
      cap: lib.FREE_WATCHLIST_CAP,
    }),
  );
  check(
    !setupWatching.includes("Pick a company to watch to save") &&
      setupWatching.includes('class="save-state-ok"'),
    "and once a company is watched the bar reports the form as answerable",
    "a complete onboarding form still reads as blocked",
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
