/**
 * Does adding a company actually do BOTH halves of what tracking means?
 *
 *     npm run watchlist-check
 *
 * WHY THIS EXISTS
 * ---------------
 * `user_companies` had exactly one writer for two phases —
 * scripts/track_companies.py — and that script is a thin argument parser over
 * db/watchlist.track_companies, whose whole reason to be a function rather than
 * an INSERT is the second obligation: recording the company's stored jobs as
 * already-notified for that person, in the same transaction, so their first
 * digest is not an archive dump of everything the crawler ever stored for that
 * employer.
 *
 * The picker is a SECOND writer, in a second language, in a runtime that cannot
 * call the first one. tests/test_watchlist_write.py compares the two statements
 * textually and can prove they say the same thing; only this can prove that what
 * they say is true of the live database — that the rows land, that the queue is
 * empty afterwards, and that the cap refuses the sixth company without writing
 * anything.
 *
 * WHAT IT RUNS. lib/link-user.linkUser, lib/watchlist.watchCompany /
 * unwatchCompany / watchesAnything and lib/companies.listWatchedCompanyCards —
 * imported and called, never restated, which is the discipline
 * scripts/onboarding-check.ts and scripts/badge-check.ts already follow. A check
 * that wrote its own INSERT would prove that INSERT works.
 *
 * The ONE thing it restates is the digest queue, in §5, and it says so where it
 * does it: notify/store.CANDIDATES_SQL is Python, on the other side of a boundary
 * with no bridge, so the four clauses are transcribed here with the original
 * quoted beside them. That transcription is the point of the section — it asks
 * the question the crawler's digest will ask an hour from now, of a user that
 * has just added a company holding hundreds of jobs, and the answer has to be
 * zero.
 *
 * REAL ROWS, DELETED AFTERWARDS, for the reason onboarding-check documents:
 * these functions bind the pool rather than a transaction handle, so a
 * rolled-back wrapper transaction could not see the account they are writing
 * against. The cleanup is a `finally`, keyed by the id this script generated, and
 * the last section re-counts to prove it landed. The account is created
 * `is_active = FALSE` by branch 3, so it cannot enter a digest even if the
 * cleanup failed.
 */

import { config } from "dotenv";
import { randomUUID } from "node:crypto";
import type { User } from "@supabase/supabase-js";

config({ path: ".env.local" });

/** Deferred imports — see onboarding-check.ts: lib/db builds its pool at module scope. */
type Lib = {
  sql: typeof import("../src/lib/db").sql;
  linkUser: typeof import("../src/lib/link-user").linkUser;
  watchCompany: typeof import("../src/lib/watchlist").watchCompany;
  unwatchCompany: typeof import("../src/lib/watchlist").unwatchCompany;
  watchesAnything: typeof import("../src/lib/watchlist").watchesAnything;
  watchlistState: typeof import("../src/lib/watchlist").watchlistState;
  WatchlistFullError: typeof import("../src/lib/watchlist").WatchlistFullError;
  listWatchedCompanyCards: typeof import("../src/lib/companies").listWatchedCompanyCards;
  listCompanies: typeof import("../src/lib/companies").listCompanies;
  currentUserId: typeof import("./current-user").currentUserId;
  FREE_WATCHLIST_CAP: typeof import("../src/lib/labels").FREE_WATCHLIST_CAP;
};

async function loadLib(): Promise<Lib> {
  const [db, link, watchlist, companies, currentUser, labels] = await Promise.all([
    import("../src/lib/db"),
    import("../src/lib/link-user"),
    import("../src/lib/watchlist"),
    import("../src/lib/companies"),
    import("./current-user"),
    import("../src/lib/labels"),
  ]);
  return {
    sql: db.sql,
    linkUser: link.linkUser,
    watchCompany: watchlist.watchCompany,
    unwatchCompany: watchlist.unwatchCompany,
    watchesAnything: watchlist.watchesAnything,
    watchlistState: watchlist.watchlistState,
    WatchlistFullError: watchlist.WatchlistFullError,
    listWatchedCompanyCards: companies.listWatchedCompanyCards,
    listCompanies: companies.listCompanies,
    currentUserId: currentUser.currentUserId,
    FREE_WATCHLIST_CAP: labels.FREE_WATCHLIST_CAP,
  };
}

const NEW_EMAIL = `watchlist-check-${randomUUID().slice(0, 8)}@example.test`;

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

  // --- §1 the number on the pricing card ----------------------------------
  console.log("§1 the cap");
  check(
    lib.FREE_WATCHLIST_CAP === 5,
    `FREE_WATCHLIST_CAP is ${lib.FREE_WATCHLIST_CAP}, the number the Free tier on the landing page promises`,
    `got ${lib.FREE_WATCHLIST_CAP}; the pricing card says 5`,
  );

  // --- §2 the operator, before --------------------------------------------
  //
  // The account that has been running since Phase 1 watches all 50 catalog
  // companies and holds ~7,700 notification rows. Nothing in this slice may move
  // either number, and "unaffected" is a before/after here rather than a promise.
  console.log("\n§2 the operator, before");
  const operatorId = await lib.currentUserId();
  type Footprint = { companies: number; notifications: number };
  const footprint = async (userId: string): Promise<Footprint> => {
    const [row] = await sql<Footprint[]>`
      SELECT (SELECT count(*)::int FROM user_companies         WHERE user_id = ${userId}) AS companies,
             (SELECT count(*)::int FROM user_job_notifications WHERE user_id = ${userId}) AS notifications
    `;
    return row;
  };
  const operatorBefore = await footprint(operatorId);
  console.log(
    `  public.users ${operatorId}: watching ${operatorBefore.companies}, ` +
      `${operatorBefore.notifications} notification rows`,
  );
  check(
    operatorBefore.companies > 0,
    `it watches ${operatorBefore.companies} companies, so match-parity's 34 is computed over a full watch list`,
  );

  // The companies with the most stored history — the ones that would flood an
  // inbox, which is what makes them the right ones to test the suppression with.
  const richest = await sql<{ id: number; name: string; stored: number; queueable: number }[]>`
    SELECT c.id,
           c.name,
           count(j.canonical_id)::int AS stored,
           count(j.canonical_id) FILTER (
             WHERE j.status = 'open' AND j.seeded = FALSE
           )::int AS queueable
      FROM companies c
      JOIN jobs j ON j.company_id = c.id
     GROUP BY c.id
     ORDER BY count(j.canonical_id) DESC
     LIMIT 7
  `;
  check(richest.length >= 6, "there are at least 6 companies with stored jobs to test against");

  let newUserId = "";

  try {
    // --- §3 a brand-new account watches nothing ---------------------------
    console.log(`\n§3 a brand-new account: ${NEW_EMAIL}`);
    const authUid = randomUUID();
    const linked = await lib.linkUser(syntheticGoogleUser(authUid, NEW_EMAIL));
    newUserId = linked.id;
    check(linked.created === true, "linkUser took branch 3 — a genuinely new account");

    const fresh = await footprint(newUserId);
    check(
      fresh.companies === 0 && fresh.notifications === 0,
      "it watches nothing and has no notification history — signup creates neither",
      `companies=${fresh.companies} notifications=${fresh.notifications}`,
    );
    check(
      (await lib.watchesAnything(newUserId)) === false,
      "so PUT /api/preferences would REFUSE to finish its onboarding — an account " +
        "watching nothing matches nothing, whatever its keywords say",
    );
    check(
      (await lib.listWatchedCompanyCards(newUserId)).length === 0,
      "and Home shows it no companies at all, rather than the 50-company shared catalog",
    );

    // --- §4 the add, and its second half ----------------------------------
    console.log("\n§4 watching one company — the pair AND the suppression");
    const target = richest[0];
    const result = await lib.watchCompany(newUserId, target.id);
    console.log(
      `  ${target.name}: ${target.stored} stored jobs, of which ${target.queueable} ` +
        `are open and not back catalogue`,
    );

    check(result.inserted === true, "the pair was genuinely inserted");
    check(
      result.suppressed === target.stored,
      `and ${result.suppressed} job(s) were recorded as already-seen — EVERY stored row for ` +
        `that company, not only the open ones (a closed job that reopens keeps its identity)`,
      `suppressed ${result.suppressed}, company holds ${target.stored}`,
    );

    const [pair] = await sql<{ n: number }[]>`
      SELECT count(*)::int AS n FROM user_companies
       WHERE user_id = ${newUserId} AND company_id = ${target.id}
    `;
    check(pair.n === 1, "the user_companies row is in the database", `got ${pair.n}`);

    const afterAdd = await footprint(newUserId);
    check(
      afterAdd.notifications === target.stored,
      `user_job_notifications holds ${afterAdd.notifications} rows for this account, all written ` +
        `by the add and none of them a receipt for an email that was sent`,
      `got ${afterAdd.notifications}, expected ${target.stored}`,
    );

    // --- §5 THE PROOF THAT MATTERS: the first digest is not flooded --------
    //
    // The four clauses below are notify/store.CANDIDATES_SQL, transcribed. The
    // original:
    //
    //     WHERE j.status = 'open'
    //       AND EXISTS     (user_companies uc WHERE uc.user_id = $1
    //                                           AND uc.company_id = j.company_id)
    //       AND j.seeded = FALSE
    //       AND NOT EXISTS (user_job_notifications n WHERE n.user_id = $1
    //                                                  AND n.canonical_id = j.canonical_id)
    //
    // Restated rather than imported because it lives in Python and the two
    // runtimes share no code. What it asks here is the question the hourly cron
    // will ask an hour from now, of an account that has just started watching a
    // company holding hundreds of postings.
    console.log("\n§5 what this account's next digest would contain");
    const [queue] = await sql<{ candidates: number }[]>`
      SELECT count(*)::int AS candidates
        FROM jobs j
       WHERE j.status = 'open'
         AND EXISTS (SELECT 1 FROM user_companies uc
                      WHERE uc.user_id = ${newUserId} AND uc.company_id = j.company_id)
         AND j.seeded = FALSE
         AND NOT EXISTS (SELECT 1 FROM user_job_notifications n
                          WHERE n.user_id = ${newUserId} AND n.canonical_id = j.canonical_id)
    `;
    check(
      queue.candidates === 0,
      `ZERO candidates — the first digest is not an archive dump. Without the suppression it ` +
        `would have queued ${target.queueable} posting(s) from ${target.name} alone`,
      `${queue.candidates} job(s) are in this account's queue`,
    );
    check(
      target.queueable > 0,
      `and that number is not zero for a boring reason: ${target.name} really does hold ` +
        `${target.queueable} open, non-seeded rows that WOULD have been queued`,
      `${target.name} has no queueable rows, so §5 proves nothing — pick another company`,
    );

    // --- §6 re-adding suppresses nothing ----------------------------------
    //
    // The sharp edge from db/watchlist.track_companies: running the suppression
    // over a pair that already exists would swallow every job that arrived since
    // — somebody's actual queue, silently.
    console.log("\n§6 adding the same company again");
    const again = await lib.watchCompany(newUserId, target.id);
    check(again.inserted === false, "the second add inserts nothing — ON CONFLICT DO NOTHING");
    check(
      again.suppressed === 0,
      "and suppresses NOTHING, so the jobs that arrive between two clicks are still queued",
      `it suppressed ${again.suppressed} more`,
    );
    const afterRepeat = await footprint(newUserId);
    check(
      afterRepeat.companies === 1 && afterRepeat.notifications === afterAdd.notifications,
      "the row counts are unchanged by the repeat",
      `companies=${afterRepeat.companies} notifications=${afterRepeat.notifications}`,
    );

    // --- §7 the cap, on the server ----------------------------------------
    console.log(`\n§7 the free cap of ${lib.FREE_WATCHLIST_CAP}, enforced server-side`);
    const [account] = await sql<{ plan: string }[]>`
      SELECT plan FROM public.users WHERE id = ${newUserId}
    `;
    check(account.plan === "free", `the new account's plan is '${account.plan}'`);

    for (const company of richest.slice(1, lib.FREE_WATCHLIST_CAP)) {
      await lib.watchCompany(newUserId, company.id);
    }
    const atCap = await lib.watchlistState(newUserId);
    check(
      atCap.companyIds.length === lib.FREE_WATCHLIST_CAP,
      `it now watches ${atCap.companyIds.length}, exactly the cap`,
      `got ${atCap.companyIds.length}`,
    );
    check(atCap.cap === lib.FREE_WATCHLIST_CAP, "and the state reports that cap to the screen");

    const sixth = richest[lib.FREE_WATCHLIST_CAP];
    const beforeRefusal = await footprint(newUserId);
    let refused = false;
    let message = "";
    try {
      await lib.watchCompany(newUserId, sixth.id);
    } catch (err) {
      refused = err instanceof lib.WatchlistFullError;
      message = err instanceof Error ? err.message : String(err);
    }
    check(refused, `the SIXTH company (${sixth.name}) is refused by lib/watchlist, not by the UI`);
    check(
      /limit on your plan/.test(message) && /nothing has been changed/.test(message),
      "with a message a person can read, saying nothing was changed",
      `got: ${message}`,
    );

    const afterRefusal = await footprint(newUserId);
    check(
      afterRefusal.companies === beforeRefusal.companies &&
        afterRefusal.notifications === beforeRefusal.notifications,
      "AND THE REFUSAL WROTE NOTHING — the transaction rolled back both halves, so a refused " +
        "add cannot leave a pair behind or suppress a company's history",
      `companies ${beforeRefusal.companies}->${afterRefusal.companies}, ` +
        `notifications ${beforeRefusal.notifications}->${afterRefusal.notifications}`,
    );
    const [orphan] = await sql<{ n: number }[]>`
      SELECT count(*)::int AS n FROM user_companies
       WHERE user_id = ${newUserId} AND company_id = ${sixth.id}
    `;
    check(orphan.n === 0, "and no row for the refused company exists", `got ${orphan.n}`);

    // --- §8 Home now shows the watch list and nothing else ----------------
    console.log("\n§8 what this account's Home shows");
    const cards = await lib.listWatchedCompanyCards(newUserId);
    const catalog = await lib.listCompanies();
    check(
      cards.length === lib.FREE_WATCHLIST_CAP,
      `Home lists ${cards.length} companies — the ones it watches`,
      `got ${cards.length}`,
    );
    check(
      catalog.length > cards.length,
      `and NOT the ${catalog.length}-company shared catalog, which is what it would have shown ` +
        `before this slice`,
    );
    check(
      cards.every((card) => atCap.companyIds.includes(card.id)),
      "every company on Home is one this account actually watches",
    );

    // --- §9 removing one -------------------------------------------------
    console.log("\n§9 removing one");
    const beforeRemove = await footprint(newUserId);
    const removed = await lib.unwatchCompany(newUserId, target.id);
    const afterRemove = await footprint(newUserId);
    check(removed === true, "the pair is deleted");
    check(
      afterRemove.companies === beforeRemove.companies - 1,
      "the watch list is one shorter",
      `${beforeRemove.companies} -> ${afterRemove.companies}`,
    );
    check(
      afterRemove.notifications === beforeRemove.notifications,
      "and the suppression rows STAY — otherwise removing and re-adding would re-deliver " +
        "everything that company held in between",
      `${beforeRemove.notifications} -> ${afterRemove.notifications}`,
    );
    check(
      (await lib.unwatchCompany(newUserId, target.id)) === false,
      "removing it twice reports that there was nothing to remove",
    );
    // And the cap frees up again, which is what makes the limit usable rather
    // than a one-way ratchet.
    const readd = await lib.watchCompany(newUserId, target.id);
    check(
      readd.inserted === true && readd.suppressed === 0,
      "re-adding it is allowed again AND suppresses nothing new — every job it names is " +
        "already recorded, which is why an untrack/re-track cycle cannot flood an inbox",
      `inserted=${readd.inserted} suppressed=${readd.suppressed}`,
    );
  } finally {
    // By the id this script generated. preferences, user_companies,
    // user_job_notifications and resumes all cascade from public.users.
    if (newUserId) {
      await sql`DELETE FROM public.users WHERE id = ${newUserId}`;
    }
  }

  // --- §10 the operator, after --------------------------------------------
  console.log("\n§10 afterwards");
  const [{ strays }] = await sql<{ strays: number }[]>`
    SELECT count(*)::int AS strays FROM public.users WHERE lower(email) = lower(${NEW_EMAIL})
  `;
  check(strays === 0, "the throwaway account is gone — the cleanup ran", `${strays} left`);

  const operatorAfter = await footprint(operatorId);
  check(
    operatorAfter.companies === operatorBefore.companies,
    `the operator still watches ${operatorBefore.companies} companies`,
    `was ${operatorBefore.companies}, now ${operatorAfter.companies}`,
  );
  check(
    operatorAfter.notifications === operatorBefore.notifications,
    `and still holds ${operatorBefore.notifications} notification rows — this slice wrote ` +
      `nothing against the account whose digest is already running`,
    `was ${operatorBefore.notifications}, now ${operatorAfter.notifications}`,
  );

  console.log(
    failures
      ? `\nFAILED — ${failures} assertion(s) did not hold.`
      : "\nOK — a new account starts watching nothing, an add writes the pair AND suppresses " +
          "that company's stored history in one transaction, the next digest would carry zero " +
          "jobs, and the sixth company is refused by the server without writing anything.",
  );
  if (failures) process.exitCode = 1;
}

main()
  .catch((err) => {
    console.error("\nFAILED:", err instanceof Error ? err.message : err);
    process.exitCode = 1;
  })
  .finally(async () => {
    const { sql } = await import("../src/lib/db");
    await sql.end({ timeout: 5 });
  });
