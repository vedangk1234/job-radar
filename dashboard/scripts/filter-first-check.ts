/**
 * Does a new account's FIRST SAVE really suppress the back catalogue?
 *
 *     npm run filter-first-check
 *
 * WHY THIS EXISTS
 * ---------------
 * The filter-first slice removed one clause from notify/store.CANDIDATES_SQL —
 * `EXISTS (user_companies …)` — and in doing so removed the thing that made a
 * person's first digest safe. It was never the gate alone that did that: the gate
 * was paired with a suppression in db/watchlist.track_companies, which recorded a
 * company's stored jobs as already-notified at the moment somebody subscribed to
 * it. Take the gate away and a brand-new account matches the WHOLE CATALOG the
 * instant it has a filter, with nothing suppressing any of it.
 *
 * So the obligation moved to lib/preferences.savePreferences, and this is the
 * check that it actually discharges. The number it prints is the whole slice:
 *
 *     a fresh account saves a real filter
 *       -> N jobs recorded as already-seen
 *       -> what its first email would contain: 0
 *       -> every one of those jobs still on its Home screen
 *
 * THAT LAST LINE IS THE HALF NOTHING ELSE CHECKS, and it is the promise the slice
 * makes to a person: the back catalogue is not hidden from them, it is merely not
 * emailed at them. A suppression that also emptied Home would be a bug that every
 * "was anything sent?" assertion in this repo would pass.
 *
 * WHY IT CANNOT BE ASKED OF THE OPERATOR'S ACCOUNT — the same problem
 * scripts/watchlist-check.ts has and states. That account has been running since
 * Phase 1: it holds ~7,700 notification rows, so there is no back catalogue left
 * to suppress and the interesting number is zero for a reason that has nothing to
 * do with this code. The property is only visible on an account that has never
 * been sent anything, so this creates one.
 *
 * WHAT IT RUNS. lib/link-user.linkUser, lib/preferences.savePreferences and
 * lib/jobs.matchCountsByCompany / matchCandidates — imported and called, never
 * restated, which is the discipline onboarding-check and badge-check follow. A
 * check that wrote its own INSERT would prove that INSERT works.
 *
 * The ONE thing it restates is the digest queue, in `emailable()`, and it says so
 * where it does it: notify/store.CANDIDATES_SQL is Python, on the other side of a
 * boundary with no bridge, so its clauses are transcribed there. Read that
 * function's comment before changing anything here — the queue applies NO filter,
 * so a healthy new account's raw queue is ~2,000 rows and always was. What has to
 * be zero is the queue INTERSECTED with the matcher, which is what a person
 * receives, and conflating the two is what broke the first draft of this check.
 *
 * REAL ROWS, DELETED AFTERWARDS, for the reason onboarding-check documents:
 * savePreferences binds the pool rather than a transaction handle, so a
 * rolled-back wrapper transaction could not see the account it is writing
 * against. A rollback would also prove the wrong thing — the claim under test is
 * that a real save really writes suppression rows. The cleanup is a `finally`,
 * keyed by the id this script generated, and §6 re-counts to prove it landed. The
 * account is created `is_active = FALSE` by branch 3, so it cannot enter a digest
 * even if the cleanup failed.
 */

import { config } from "dotenv";
import { randomUUID } from "node:crypto";
import type { User } from "@supabase/supabase-js";

config({ path: ".env.local" });

/** Deferred imports — see onboarding-check.ts: lib/db builds its pool at module scope. */
type Lib = {
  sql: typeof import("../src/lib/db").sql;
  linkUser: typeof import("../src/lib/link-user").linkUser;
  loadPreferences: typeof import("../src/lib/preferences").loadPreferences;
  savePreferences: typeof import("../src/lib/preferences").savePreferences;
  matchCandidates: typeof import("../src/lib/jobs").matchCandidates;
  matchCountsByCompany: typeof import("../src/lib/jobs").matchCountsByCompany;
  isMatch: typeof import("../src/lib/match").isMatch;
  currentUserId: typeof import("./current-user").currentUserId;
};

async function loadLib(): Promise<Lib> {
  const [db, link, preferences, jobs, match, currentUser] = await Promise.all([
    import("../src/lib/db"),
    import("../src/lib/link-user"),
    import("../src/lib/preferences"),
    import("../src/lib/jobs"),
    import("../src/lib/match"),
    import("./current-user"),
  ]);
  return {
    sql: db.sql,
    linkUser: link.linkUser,
    loadPreferences: preferences.loadPreferences,
    savePreferences: preferences.savePreferences,
    matchCandidates: jobs.matchCandidates,
    matchCountsByCompany: jobs.matchCountsByCompany,
    isMatch: match.isMatch,
    currentUserId: currentUser.currentUserId,
  };
}

const NEW_EMAIL = `filter-first-check-${randomUUID().slice(0, 8)}@example.test`;

/**
 * The filter the throwaway account saves, and it is chosen to MATCH SOMETHING.
 *
 * A check whose headline number is "N suppressed" proves nothing at N = 0, and
 * every fail-closed default in this app produces exactly that: a blank city
 * answer, an empty work_modes and an unconfigured range each match no job at all.
 * So these are the operator's own live answers rather than something minimal —
 * `blr` and a keyword the board actually carries — and §3 refuses to continue if
 * the resulting match set is empty, rather than printing a green zero.
 */
const SAVED_FILTER = {
  cities: ["blr"],
  selected_cities: ["blr"],
  all_india: false,
  keywords: ["backend"],
  exclusions: [] as string[],
  max_level: 2,
  exp_range_low: 0,
  exp_range_high: 2,
  years_of_experience_held: null,
  show_no_experience_jobs: true,
  work_modes: ["onsite", "hybrid", "remote"],
  salary_bands: [] as string[],
};

let failures = 0;

function check(ok: boolean, label: string, detail = "") {
  console.log(`  ${ok ? "ok  " : "FAIL"}  ${label}${detail && !ok ? `\n        ${detail}` : ""}`);
  if (!ok) failures += 1;
}

/** The shape linkUser reads. Not a session and not a forgery — see onboarding-check. */
function syntheticGoogleUser(id: string, email: string): User {
  return {
    id,
    email,
    email_confirmed_at: new Date().toISOString(),
    user_metadata: { email_verified: true },
    identities: [{ provider: "google", identity_data: { email, email_verified: true } }],
  } as unknown as User;
}

async function main() {
  const lib = await loadLib();
  const { sql } = lib;

  // --- §1 the operator, before --------------------------------------------
  //
  // The account that has been running since Phase 1 holds ~7,700 notification
  // rows. Nothing in this script may move that number, and "unaffected" is a
  // before/after here rather than a promise — this script creates an account and
  // writes suppression rows, which is exactly the shape of thing that writes
  // against the wrong user_id when a parameter is bound wrongly.
  console.log("§1 the operator, before");
  const operatorId = await lib.currentUserId();
  const operatorBefore = await countRows(sql, operatorId);
  console.log(
    `  ${operatorId}: ${operatorBefore.notifications} notification row(s), ` +
      `${operatorBefore.companies} pinned compan(y/ies)`,
  );

  let newUserId = "";
  try {
    // --- §2 a brand-new account -------------------------------------------
    //
    // Through linkUser rather than an INSERT, so the row under test is the row a
    // real Google sign-in produces: branch 3, a blank preferences row from
    // NEW_ACCOUNT_SEED, is_active FALSE.
    const authId = randomUUID();
    const linked = await lib.linkUser(syntheticGoogleUser(authId, NEW_EMAIL));
    newUserId = linked.id;
    console.log(`\n§2 a brand-new account: ${NEW_EMAIL}`);
    check(linked.created, "linkUser took branch 3 — a genuinely new account");

    const blank = await lib.loadPreferences(newUserId);
    check(
      blank.onboarding_completed === false,
      "it starts behind the onboarding gate",
    );

    // THE PROPERTY THE WHOLE DELTA RULE RESTS ON. A blank row matches nothing —
    // no city answer, empty work_modes, unconfigured range, all three fail-closed
    // — which is why "day one" needs no special case: OLD is empty, so NEW \ OLD
    // is NEW. If a future edit made any of those blanks permissive, the first
    // save would suppress only the difference and the rest would be emailed.
    const blankMatches = (await lib.matchCandidates(blank, newUserId)).filter((job) =>
      lib.isMatch(job, blank),
    );
    check(
      blankMatches.length === 0,
      "and its blank filter matches NOTHING — which is what makes day one a delta",
      `a blank row matched ${blankMatches.length} job(s); the first save would then ` +
        `suppress only the difference and email the rest`,
    );

    const emailableBefore = await emailable(lib, newUserId, blank);
    check(
      emailableBefore === 0,
      "and nothing at all would be emailed to it before it has a filter",
      `${emailableBefore} job(s) would be sent`,
    );

    // --- §3 what the save is about to admit --------------------------------
    console.log("\n§3 what that filter admits, measured before the save");
    // TWO DIFFERENT NUMBERS, AND CONFLATING THEM IS WHAT BROKE THE FIRST DRAFT.
    //
    //   onScreen   every OPEN row the filter matches — what Home renders. It
    //              includes SEEDED rows, because Home answers "what fits me right
    //              now" and a silently-seeded posting still fits.
    //   wouldSend  the subset that is also queue-eligible: not seeded, not already
    //              recorded. This is what an email would contain.
    //
    // Only the second can be suppressed, because only the second was ever going to
    // be sent — `j.seeded = FALSE` keeps a back catalogue out of every queue
    // forever, so recording those rows would cost one per user per seeded job and
    // buy nothing. The gap between the two is the silent seed, and it is expected
    // to be large on this board: ~7,700 of ~7,900 open rows are seeded.
    const onScreen = (
      await lib.matchCandidates(SAVED_FILTER as never, newUserId)
    ).filter((job) => lib.isMatch(job, SAVED_FILTER as never)).length;
    const wouldSend = await emailable(lib, newUserId, SAVED_FILTER as never);
    console.log(`  ${onScreen} open job(s) match it — of which ${wouldSend} are emailable`);
    console.log(`  (the other ${onScreen - wouldSend} are back catalogue: seeded, never sent to anybody)`);
    if (wouldSend === 0) {
      throw new Error(
        "the filter this check saves has nothing emailable to suppress, so a count of " +
          "zero would be indistinguishable from a broken suppression. Widen " +
          "SAVED_FILTER until it matches a non-seeded open job.",
      );
    }

    // --- §4 the save, and the number the slice turns on --------------------
    console.log("\n§4 the first save — the filter, and the history it admits");
    const { preferences: saved, suppressed } = await lib.savePreferences(
      newUserId,
      SAVED_FILTER,
    );
    console.log(`  ${suppressed} job(s) recorded as already-seen by that one save`);

    check(saved.onboarding_completed === true, "the save released the onboarding gate");
    check(
      suppressed >= wouldSend,
      "it suppressed at least every EMAILABLE job the filter matches",
      `suppressed ${suppressed}, but ${wouldSend} were emailable`,
    );
    // STRICTLY MORE, ALMOST CERTAINLY, AND THAT IS THE STATUS-BLIND HALF. The
    // count above is open rows only; the suppression covers closed ones too,
    // because a closed job that reopens keeps its canonical_id and would
    // otherwise arrive as news the week a vendor re-lists it. On a board with any
    // history at all the two numbers differ, and the difference is the flap hole
    // being closed. Reported rather than asserted: a database whose every match is
    // open is not wrong, it is just young.
    console.log(
      `  of which ${suppressed - wouldSend} are CLOSED rows — the flap hole, closed`,
    );

    // THE DIGEST'S OWN QUESTION, TRANSCRIBED. notify/store.CANDIDATES_SQL is:
    //
    //     WHERE j.status = 'open'
    //       AND j.seeded = FALSE
    //       AND NOT EXISTS (user_job_notifications n WHERE n.user_id = $1
    //                                                  AND n.canonical_id = j.canonical_id)
    //
    // Asked of this account, an hour before the cron would ask it. Note it is the
    // full queue rather than the matched set: if this is zero, no filter can make
    // it non-zero, which is a stronger statement than "the matcher found nothing".
    const sendAfter = await emailable(lib, newUserId, saved);
    check(
      sendAfter === 0,
      "AND ITS FIRST EMAIL WOULD CONTAIN NOTHING — the archive dump is suppressed",
      `${sendAfter} job(s) would be sent; that is what would have arrived`,
    );

    // --- §5 the half that must NOT be suppressed ---------------------------
    //
    // The promise this slice makes is "the dashboard shows them, the digest does
    // not email them". Everything above is the second half. This is the first,
    // and it is the one a suppression bug would break silently in the other
    // direction — an account whose Home is empty on day one looks like a broken
    // crawler rather than a broken filter.
    console.log("\n§5 and the dashboard still shows every one of them");
    const counts = await lib.matchCountsByCompany(saved, newUserId);
    const shown = [...counts.values()].reduce((n, c) => n + c.match, 0);
    console.log(`  Home renders ${shown} match(es) across ${counts.size} compan(y/ies)`);
    check(
      shown === onScreen,
      "Home shows exactly the jobs the filter matches — suppression hid nothing",
      `${shown} on Home against ${onScreen} matching`,
    );
    check(
      counts.size > 1,
      "across more than one company, which is the catalog-wide half",
      `only ${counts.size} company had matches; a watch-list gate would look like this`,
    );

    // --- §6 a job arriving afterwards still gets through -------------------
    //
    // The snapshot property. Suppressing "everything the filter admits" is only
    // correct if it happens once, at the save; a job stored afterwards is the
    // whole point of having a filter. Without this the suppression would be an
    // off switch nobody could see, and every assertion above would still pass.
    console.log("\n§6 a job stored after the save still reaches the queue");
    const probeId = `filter-first-check:${randomUUID()}`;
    const [company] = await sql<{ id: number }[]>`
      SELECT id FROM companies ORDER BY id LIMIT 1
    `;
    try {
      await sql`
        INSERT INTO jobs (canonical_id, company_id, title, location, city, url,
                          first_seen_at, last_seen_at, status, seeded)
        VALUES (${probeId}, ${company.id}, 'Backend Engineer', 'Bengaluru, India',
                'blr', 'https://example.test/probe', now(), now(), 'open', FALSE)
      `;
      const sendWithProbe = await emailable(lib, newUserId, saved);
      check(
        sendWithProbe === 1,
        "exactly one job would be sent — the suppression was a snapshot, not an off switch",
        `${sendWithProbe} would be sent; expected the probe and nothing else`,
      );
    } finally {
      // Deleted whatever happened above, including a failed assertion: a
      // fabricated job left in `jobs` is a row the real crawler never saw and
      // would close in three cycles, having briefly been in a real digest.
      await sql`DELETE FROM jobs WHERE canonical_id = ${probeId}`;
    }
  } finally {
    if (newUserId) {
      // Keyed by the id this script generated, never by email pattern: a DELETE
      // matching a LIKE could reach a real person who happened to sign up with a
      // similar address. The cascades take preferences and the suppression rows.
      await sql`DELETE FROM public.users WHERE id = ${newUserId}`;
    }
  }

  // --- §7 the operator, after ---------------------------------------------
  console.log("\n§7 afterwards");
  const operatorAfter = await countRows(sql, operatorId);
  check(
    operatorAfter.notifications === operatorBefore.notifications &&
      operatorAfter.companies === operatorBefore.companies,
    "the operator's rows are untouched",
    `notifications ${operatorBefore.notifications} -> ${operatorAfter.notifications}, ` +
      `companies ${operatorBefore.companies} -> ${operatorAfter.companies}`,
  );
  const [{ strays }] = await sql<{ strays: number }[]>`
    SELECT count(*)::int AS strays FROM public.users WHERE email = ${NEW_EMAIL}
  `;
  check(strays === 0, "and the throwaway account is gone", `${strays} row(s) left behind`);

  console.log(
    failures === 0
      ? "\nfilter-first-check: all assertions passed"
      : `\nfilter-first-check: ${failures} FAILED`,
  );
  await sql.end({ timeout: 5 });
  process.exit(failures === 0 ? 0 : 1);
}

/**
 * WHAT WOULD ACTUALLY BE EMAILED: the queue, narrowed by the filter.
 *
 * THE FIRST DRAFT OF THIS CHECK COUNTED THE QUEUE ALONE AND WAS WRONG IN A WAY
 * WORTH RECORDING, because the mistake is the easiest one to make about this
 * codebase. `notify/store.CANDIDATES_SQL` is NOT a match query — it applies no
 * filter at all. It answers "what is eligible to be considered": open, not back
 * catalogue, not already told. `run_digest` then runs `notify.match.is_match`
 * over those rows, and only the survivors are sent.
 *
 * So a brand-new account's raw queue is ~2,000 rows and always was, under
 * filter-first and under the watch-list gate alike. Asserting it is zero asserts
 * something false about a healthy system. What has to be zero is the INTERSECTION,
 * which is what a person receives.
 *
 * The two clauses below are CANDIDATES_SQL transcribed — it is Python, and the
 * runtimes share no code — and the filter is then applied in TypeScript by the
 * same `isMatch` the digest's Python twin runs, which `npm run match-parity`
 * holds to the Python over every live row.
 */
async function emailable(
  lib: Lib,
  userId: string,
  prefs: Parameters<Lib["isMatch"]>[1],
): Promise<number> {
  const rows = await lib.sql<
    {
      canonical_id: string;
      title: string;
      city: string | null;
      work_mode: string | null;
      min_experience: number | null;
      is_pm_role: boolean;
      desc_checked: boolean;
      desc_fetch_failed: boolean;
      salary_min: string | null;
      salary_max: string | null;
    }[]
  >`
    SELECT j.canonical_id, j.title, j.city, j.work_mode,
           j.min_experience, j.is_pm_role, j.desc_checked,
           j.desc_fetch_failed, j.salary_min, j.salary_max
      FROM jobs j
     WHERE j.status = 'open'
       AND j.seeded = FALSE
       AND NOT EXISTS (
             SELECT 1
               FROM user_job_notifications n
              WHERE n.user_id = ${userId}
                AND n.canonical_id = j.canonical_id
           )
  `;
  return rows.filter((job) => lib.isMatch(job, prefs)).length;
}

async function countRows(sql: Lib["sql"], userId: string) {
  const [row] = await sql<{ notifications: number; companies: number }[]>`
    SELECT
      (SELECT count(*)::int FROM user_job_notifications WHERE user_id = ${userId}) AS notifications,
      (SELECT count(*)::int FROM user_companies         WHERE user_id = ${userId}) AS companies
  `;
  return row;
}

main().catch(async (err) => {
  console.error("\nfilter-first-check FAILED:", err);
  process.exit(1);
});
