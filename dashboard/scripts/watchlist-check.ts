/**
 * Does pinning a company write the pair, hold the cap, and touch nothing else?
 *
 *     npm run watchlist-check
 *
 * WHY THIS EXISTS, AND WHAT IT STOPPED BEING
 * ------------------------------------------
 * It used to ask whether adding a company did BOTH halves of what tracking meant:
 * the pair, and the suppression of that company's stored history in the same
 * transaction. Its headline was "1,180 and 0" — adding Continental to a fresh
 * account recorded 1,180 rows as already-seen and left the queue at zero, where
 * without the second half 212 open postings would have been that account's first
 * email.
 *
 * The filter-first slice removed the watch-list gate from the digest queue, and
 * the suppression went with it: a pair admits nothing now, so suppressing on the
 * strength of one takes away jobs a person's FILTER already earned them. The
 * hazard did not go away — it moved to the filter, and so did the check.
 * `npm run filter-first-check` is where the "N and 0" number lives now.
 *
 * WHAT IS LEFT HERE IS STILL WORTH A CHECK, and it is the half that was never
 * about suppression: the pair lands, the per-plan cap is enforced on the server
 * and refuses the sixth company WITHOUT WRITING ANYTHING (the refusal is a throw
 * inside the transaction that had already inserted the row), unwatching removes
 * exactly one row, and none of it disturbs the operator.
 *
 * WHAT IT RUNS. lib/link-user.linkUser and lib/watchlist.watchCompany /
 * unwatchCompany / watchlistState — imported and called, never restated, which is
 * the discipline scripts/onboarding-check.ts and scripts/badge-check.ts already
 * follow. A check that wrote its own INSERT would prove that INSERT works.
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
  watchlistState: typeof import("../src/lib/watchlist").watchlistState;
  WatchlistFullError: typeof import("../src/lib/watchlist").WatchlistFullError;
  listCompanyCards: typeof import("../src/lib/companies").listCompanyCards;
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
    watchlistState: watchlist.watchlistState,
    WatchlistFullError: watchlist.WatchlistFullError,
    listCompanyCards: companies.listCompanyCards,
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

/**
 * The digest's own queue for one account, counted.
 *
 * notify/store.CANDIDATES_SQL transcribed — it is Python, and the two runtimes
 * share no code. Two clauses since the filter-first slice; the `EXISTS
 * (user_companies …)` gate that stood first is what this script exists to prove
 * is gone, so its absence here is deliberate rather than an omission.
 */
async function digestQueue(sql: Lib["sql"], userId: string): Promise<number> {
  const [{ queued }] = await sql<{ queued: number }[]>`
    SELECT count(*)::int AS queued
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
  return queued;
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
    // PUT /api/preferences used to REFUSE to finish this account's onboarding
    // for exactly this state, and the filter-first slice removed that refusal —
    // an account pinning nothing now matches the whole catalog, so there was no
    // missing answer to refuse for. Home correspondingly shows it every company
    // rather than none, which is what the last check here says.
    check(
      (await lib.listCompanyCards(newUserId)).length > 0,
      "and Home shows it the whole catalog — pinning nothing is not a narrowed view",
      "an account that pins nothing sees no companies; the watch-list gate is back",
    );

    // --- §4 the add — the pair, AND NOTHING ELSE --------------------------
    //
    // "Nothing else" is the assertion that changed. This section used to check
    // that the add ALSO wrote a notification row per stored job; it now checks
    // that it writes NONE, because a pin no longer admits anything and
    // suppressing on one would remove jobs the person's filter already earned.
    // See lib/watchlist.watchCompany for the sequence that makes that a real bug
    // rather than harmless insurance.
    console.log("\n§4 pinning one company — the pair, and nothing else");
    const target = richest[0];
    const queueBefore = await digestQueue(sql, newUserId);
    const result = await lib.watchCompany(newUserId, target.id);
    const queueAfterPin = await digestQueue(sql, newUserId);
    console.log(
      `  ${target.name}: ${target.stored} stored jobs, of which ${target.queueable} ` +
        `are open and not back catalogue`,
    );

    check(result.inserted === true, "the pair was genuinely inserted");

    const [pair] = await sql<{ n: number }[]>`
      SELECT count(*)::int AS n FROM user_companies
       WHERE user_id = ${newUserId} AND company_id = ${target.id}
    `;
    check(pair.n === 1, "the user_companies row is in the database", `got ${pair.n}`);

    const afterAdd = await footprint(newUserId);
    check(
      afterAdd.notifications === 0,
      `and NOT ONE notification row was written, though ${target.name} holds ` +
        `${target.stored} stored jobs — pinning withholds nothing`,
      `got ${afterAdd.notifications} notification row(s); watchCompany is suppressing ` +
        `history again, which silently removes jobs this account's filter already matched`,
    );

    // --- §5 the queue is indifferent to the pin ---------------------------
    //
    // THE SECTION THAT INVERTED. It used to be "THE PROOF THAT MATTERS": the
    // digest queue for this account had to be ZERO after the add, because the
    // suppression had just recorded everything the newly-watched company held.
    //
    // The queue does not read user_companies at all now, so pinning cannot change
    // it in either direction — and that is what is asserted instead. The clauses
    // below are notify/store.CANDIDATES_SQL, transcribed, minus the gate that went:
    //
    //     WHERE j.status = 'open'
    //       AND j.seeded = FALSE
    //       AND NOT EXISTS (user_job_notifications n WHERE n.user_id = $1
    //                                                  AND n.canonical_id = j.canonical_id)
    //
    // Restated rather than imported because it lives in Python and the two
    // runtimes share no code. What made the queue safe for a new account is now
    // the filter save, and npm run filter-first-check is what proves it.
    console.log("\n§5 the digest queue, before and after the pin");
    check(
      queueBefore === queueAfterPin,
      `pinning changed nothing about the queue (${queueBefore} both sides) — it is ` +
        `the filter that decides, and this account has not saved one`,
      `${queueBefore} -> ${queueAfterPin}: pinning moved the queue, so something is ` +
        `reading user_companies to decide an email again`,
    );

    // --- §6 re-adding writes nothing --------------------------------------
    //
    // ON CONFLICT DO NOTHING, and RETURNING is what makes the report honest. The
    // sharp edge this used to guard — running the suppression over a pair that
    // already existed would swallow every job that arrived since — is gone with
    // the suppression, but the idempotence is still the table's UNIQUE doing real
    // work and a second insert would be a real bug.
    console.log("\n§6 pinning the same company again");
    const again = await lib.watchCompany(newUserId, target.id);
    check(again.inserted === false, "the second add inserts nothing — ON CONFLICT DO NOTHING");
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

    // --- §8 Home shows the catalog, pinned or not -------------------------
    //
    // THE OTHER SECTION THAT INVERTED. It asserted that Home listed exactly the
    // five pinned companies and NOT the shared catalog, which was right while the
    // digest was computed from the watch list: a "for you" count over a company
    // you did not watch counted jobs that could never arrive.
    //
    // A match at any company can reach any inbox now, so the honest list is the
    // catalog — and the count beside it was always computed over the catalog
    // anyway (lib/jobs.matchCountsByCompany has never been watch-list-scoped), so
    // this is the page agreeing with its own figures. An account at the cap must
    // see MORE than five companies, not exactly five.
    console.log("\n§8 what this account's Home shows");
    const cards = await lib.listCompanyCards(newUserId);
    check(
      cards.length > lib.FREE_WATCHLIST_CAP,
      `Home lists ${cards.length} companies — the whole catalog, not the ` +
        `${lib.FREE_WATCHLIST_CAP} it pinned`,
      `got ${cards.length}, which is the watch list rather than the catalog`,
    );
    check(
      atCap.companyIds.every((id) => cards.some((card) => card.id === id)),
      "including every company it did pin — nothing was hidden by widening the list",
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
      "and it touches no notification row in either direction",
      `${beforeRemove.notifications} -> ${afterRemove.notifications}`,
    );
    check(
      (await lib.unwatchCompany(newUserId, target.id)) === false,
      "removing it twice reports that there was nothing to remove",
    );
    // And the cap frees up again, which is what makes the limit usable rather
    // than a one-way ratchet.
    const readd = await lib.watchCompany(newUserId, target.id);
    const afterReadd = await footprint(newUserId);
    check(
      readd.inserted === true,
      "re-pinning it is allowed again, so the cap is a limit rather than a one-way ratchet",
      `inserted=${readd.inserted}`,
    );
    check(
      afterReadd.notifications === beforeRemove.notifications,
      "and the unpin/re-pin cycle wrote no notification row at any point — the thing " +
        "that used to make this cycle dangerous is not there to be got wrong",
      `${beforeRemove.notifications} -> ${afterReadd.notifications}`,
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
