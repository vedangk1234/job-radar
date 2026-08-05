/**
 * Prove account deletion against the live database, on accounts it creates.
 *
 *     npm run delete-check
 *
 * WHY THIS ONE DOES NOT ROLL BACK, WHEN badge-check DOES
 * ------------------------------------------------------
 * scripts/badge-check.ts creates its second account inside a transaction and
 * rolls it back unconditionally, because what it writes touches SHARED state —
 * notification rows against real jobs — and it only needs the rows to exist for
 * the length of a query.
 *
 * A rollback here would prove the wrong thing. The claim under test is that
 * deletion COMMITS: that the rows are gone from the database and not merely from
 * one uncommitted snapshot. Wrapping it would make the test pass identically
 * whether the delete worked or whether ROLLBACK undid a failure — and lib/account
 * opens its own transaction anyway, so an outer one would be a second BEGIN on
 * the same pooled connection with an inner COMMIT of ambiguous scope. The thing
 * being verified is a commit, so it has to be allowed to commit.
 *
 * WHAT MAKES THAT SAFE, AND IT IS NOT CARE
 * ----------------------------------------
 *   - EVERY row this writes is one it created, under fixed ids in a reserved
 *     range and @example.test addresses (RFC 6761, the domains the pre-commit
 *     hook allows). It reads real companies and jobs to point at, and writes to
 *     neither.
 *   - The ids are FIXED rather than random, which is what makes cleanup possible
 *     at all: §0 removes leftovers before starting and the `finally` removes
 *     them after, so a run killed halfway is cleaned by the next run rather than
 *     leaving a row nobody can name.
 *   - NO DELETE IN THIS FILE IS EVER AIMED AT A REAL ACCOUNT — including §5,
 *     which proves you cannot delete somebody else's. The obvious way to write
 *     that is to point the delete at the operator's account with the wrong
 *     identity and check it is refused, and that is one broken predicate away
 *     from being the exact catastrophe it is testing for. It uses two throwaway
 *     accounts and aims A's delete at B's identity instead.
 *   - §6 re-reads the real account and asserts every count is what §1 recorded.
 *
 * THE THROWAWAY IS SHAPED LIKE THE PHASE-1 ACCOUNT, deliberately: its
 * public.users.id and its auth_user_id are DIFFERENT uuids. Every account created
 * since 2a has them equal, so a check built on one of those would pass just as
 * happily if the auth identity were deleted by the wrong id — the one shape that
 * catches that bug is the one the operator's own account has, and this reproduces
 * it without going near it.
 */

import { config as loadEnv } from "dotenv";

loadEnv({ path: ".env.local" });

/**
 * Deferred, like badge-check's: src/lib/db.ts builds its pool at module scope and
 * a static import is hoisted above loadEnv(), so it would evaluate
 * dashboardDbUrl() against an empty process.env and throw before the first
 * statement. Importing the app's own module is the point — this runs the
 * PRODUCTION deleteAccount(), not a restatement of it.
 */
type Lib = {
  sql: typeof import("../src/lib/db").sql;
  deleteAccount: typeof import("../src/lib/account").deleteAccount;
  accountFootprint: typeof import("../src/lib/account").accountFootprint;
};

async function loadLib(): Promise<Lib> {
  const [db, account] = await Promise.all([import("../src/lib/db"), import("../src/lib/account")]);
  return {
    sql: db.sql,
    deleteAccount: account.deleteAccount,
    accountFootprint: account.accountFootprint,
  };
}

/**
 * The two throwaway accounts, by fixed id.
 *
 * `accountId` !== `authId` for both, mirroring the Phase-1 asymmetry — see the
 * header. The addresses are .test, which is reserved by RFC 6761 and can never
 * belong to anybody.
 */
const A = {
  accountId: "00000000-0000-4000-8000-00000000dea1",
  authId: "00000000-0000-4000-8000-00000000dea2",
  email: "delete-check-a@example.test",
};
const B = {
  accountId: "00000000-0000-4000-8000-00000000deb1",
  authId: "00000000-0000-4000-8000-00000000deb2",
  email: "delete-check-b@example.test",
};
const THROWAWAYS = [A, B];

let failures = 0;

function check(ok: boolean, label: string, detail = "") {
  console.log(`  ${ok ? "ok  " : "FAIL"}  ${label}${detail && !ok ? `\n        ${detail}` : ""}`);
  if (!ok) failures += 1;
}

type Counts = { preferences: number; companies: number; notifications: number };

async function countsFor(sql: Lib["sql"], userId: string): Promise<Counts> {
  const [row] = await sql<Counts[]>`
    SELECT
      (SELECT count(*)::int FROM preferences            WHERE user_id = ${userId}) AS preferences,
      (SELECT count(*)::int FROM user_companies         WHERE user_id = ${userId}) AS companies,
      (SELECT count(*)::int FROM user_job_notifications WHERE user_id = ${userId}) AS notifications
  `;
  return row;
}

async function accountExists(sql: Lib["sql"], userId: string): Promise<boolean> {
  const [row] = await sql<{ n: number }[]>`
    SELECT count(*)::int AS n FROM public.users WHERE id = ${userId}
  `;
  return row.n > 0;
}

async function authUserExists(sql: Lib["sql"], authId: string): Promise<boolean> {
  const [row] = await sql<{ n: number }[]>`
    SELECT count(*)::int AS n FROM auth.users WHERE id = ${authId}
  `;
  return row.n > 0;
}

/** Remove every trace of the throwaways. Idempotent, and safe to run first. */
async function scrub(sql: Lib["sql"]) {
  for (const who of THROWAWAYS) {
    await sql`DELETE FROM public.users WHERE id = ${who.accountId} OR lower(email) = ${who.email}`;
    await sql`DELETE FROM auth.users   WHERE id = ${who.authId}`;
  }
}

/** One throwaway account with one of everything a real account has. */
async function seed(
  sql: Lib["sql"],
  who: typeof A,
  companyId: number,
  canonicalId: string,
): Promise<void> {
  // The identity first: an auth.users row needs only an id (everything else on
  // that table is nullable or defaulted), but email/aud/role are set so the row
  // is coherent rather than a fragment. It has no identity and no password, so
  // it is not an account anybody could sign in to even for the seconds it lives.
  await sql`
    INSERT INTO auth.users (id, email, aud, role, created_at, updated_at)
    VALUES (${who.authId}, ${who.email}, 'authenticated', 'authenticated', now(), now())
  `;
  await sql`
    INSERT INTO public.users (id, email, auth_user_id, is_active)
    VALUES (${who.accountId}, ${who.email}, ${who.authId}, FALSE)
  `;
  await sql`
    INSERT INTO preferences (user_id, cities, keywords, exclusions, max_level)
    VALUES (${who.accountId}, ARRAY['Bengaluru']::text[], ARRAY['engineer']::text[],
            ARRAY['intern']::text[], 2)
  `;
  await sql`
    INSERT INTO user_companies (user_id, company_id) VALUES (${who.accountId}, ${companyId})
  `;
  await sql`
    INSERT INTO user_job_notifications (user_id, canonical_id)
    VALUES (${who.accountId}, ${canonicalId})
  `;
}

async function main() {
  const { sql, deleteAccount, accountFootprint } = await loadLib();

  try {
    console.log("\n§0  clearing any throwaway left by an interrupted run");
    await scrub(sql);
    for (const who of THROWAWAYS) {
      check(!(await accountExists(sql, who.accountId)), `${who.email}: no public.users row`);
      check(!(await authUserExists(sql, who.authId)), `${who.email}: no auth.users row`);
    }

    // ---------------------------------------------------------------------
    console.log("\n§1  the real accounts, before anything is written");
    const real = await sql<{ id: string; email: string; auth_user_id: string | null }[]>`
      SELECT id, email, auth_user_id FROM public.users ORDER BY created_at
    `;
    check(real.length > 0, `${real.length} real account(s) found`);

    const before = new Map<string, Counts>();
    for (const row of real) {
      const counts = await countsFor(sql, row.id);
      before.set(row.id, counts);
      console.log(
        `        ${row.id}  preferences=${counts.preferences} ` +
          `companies=${counts.companies} notifications=${counts.notifications}` +
          (row.auth_user_id && row.auth_user_id !== row.id ? "  (id ≠ auth_user_id)" : ""),
      );
    }
    const [catalogue] = await sql<{ companies: number; jobs: number }[]>`
      SELECT (SELECT count(*)::int FROM companies) AS companies,
             (SELECT count(*)::int FROM jobs)      AS jobs
    `;

    // ---------------------------------------------------------------------
    console.log("\n§2  two throwaway accounts, each with one of everything");
    const [{ id: companyId }] = await sql<{ id: number }[]>`SELECT id FROM companies LIMIT 1`;
    const [{ canonical_id: canonicalId }] =
      await sql<{ canonical_id: string }[]>`SELECT canonical_id FROM jobs LIMIT 1`;

    for (const who of THROWAWAYS) await seed(sql, who, companyId, canonicalId);

    for (const who of THROWAWAYS) {
      const counts = await countsFor(sql, who.accountId);
      check(
        (await accountExists(sql, who.accountId)) &&
          (await authUserExists(sql, who.authId)) &&
          counts.preferences === 1 &&
          counts.companies === 1 &&
          counts.notifications === 1,
        `${who.email}: seeded in both tables with a filter, a watch list and a receipt`,
        JSON.stringify(counts),
      );
    }

    // The footprint the confirmation screen shows, read for a real account shape.
    const footprint = await accountFootprint(A.accountId);
    check(
      footprint.companies === 1 && footprint.notifications === 1,
      "accountFootprint() reports what the account actually holds",
      JSON.stringify(footprint),
    );

    // ---------------------------------------------------------------------
    console.log("\n§3  a user cannot delete another user's account");
    // A's account id with B's auth identity. The predicate requires BOTH to
    // match the same row, so this is refused — and both accounts are throwaways,
    // so a broken predicate destroys nothing that matters. See the header.
    let refused = false;
    try {
      await deleteAccount(A.accountId, B.authId);
    } catch (err) {
      refused = err instanceof Error && err.name === "AccountNotFoundError";
      if (!refused) throw err;
    }
    check(refused, "deleting A's account with B's identity is refused");
    check(await accountExists(sql, A.accountId), "  ...and A's account still exists");
    check(await authUserExists(sql, B.authId), "  ...and B's identity still exists");
    const aAfterRefusal = await countsFor(sql, A.accountId);
    check(
      aAfterRefusal.preferences === 1 &&
        aAfterRefusal.companies === 1 &&
        aAfterRefusal.notifications === 1,
      "  ...and none of A's rows were touched by the refused delete",
      JSON.stringify(aAfterRefusal),
    );

    // ---------------------------------------------------------------------
    console.log("\n§4  the real delete, committed");
    const receipt = await deleteAccount(A.accountId, A.authId);
    console.log(`        receipt: ${JSON.stringify(receipt)}`);
    check(receipt.publicUsers === 1, "one public.users row deleted");
    check(receipt.authUsers === 1, "one auth.users identity deleted");
    check(
      receipt.preferences === 1 && receipt.userCompanies === 1 && receipt.userJobNotifications === 1,
      "the receipt names the per-user rows that went with it",
    );

    check(!(await accountExists(sql, A.accountId)), "public.users row is gone");
    check(!(await authUserExists(sql, A.authId)), "auth.users identity is gone — no orphan");

    const after = await countsFor(sql, A.accountId);
    check(after.preferences === 0, "preferences cascaded to 0", JSON.stringify(after));
    check(after.companies === 0, "user_companies cascaded to 0", JSON.stringify(after));
    check(after.notifications === 0, "user_job_notifications cascaded to 0", JSON.stringify(after));

    // Deleting the account did not delete the shared catalogue it pointed at.
    const [catalogueAfter] = await sql<{ companies: number; jobs: number }[]>`
      SELECT (SELECT count(*)::int FROM companies) AS companies,
             (SELECT count(*)::int FROM jobs)      AS jobs
    `;
    check(
      catalogueAfter.companies === catalogue.companies && catalogueAfter.jobs === catalogue.jobs,
      `the shared catalogue is untouched (${catalogue.companies} companies, ${catalogue.jobs} jobs)`,
      JSON.stringify(catalogueAfter),
    );

    // ---------------------------------------------------------------------
    console.log("\n§5  B, deleted by its own identity, and the second half of isolation");
    check(await accountExists(sql, B.accountId), "B survived A's deletion entirely");
    const bCounts = await countsFor(sql, B.accountId);
    check(
      bCounts.preferences === 1 && bCounts.companies === 1 && bCounts.notifications === 1,
      "B kept every one of its rows while A was erased",
      JSON.stringify(bCounts),
    );
    const bReceipt = await deleteAccount(B.accountId, B.authId);
    check(bReceipt.publicUsers === 1 && bReceipt.authUsers === 1, "B deletes cleanly in turn");
    check(!(await accountExists(sql, B.accountId)), "B's account row is gone");
    check(!(await authUserExists(sql, B.authId)), "B's identity is gone");

    // COUNTED AGAIN FOR B, rather than inferred from the receipt. A clean return
    // from deleteAccount does imply it — the function re-reads all three tables
    // inside the transaction and throws CascadeFailedError if any survived — but
    // that is a claim about code the check is supposed to be testing, and a
    // check that trusts its subject to report its own success is not a check.
    // Both accounts are asked the same question in the same way.
    const bAfter = await countsFor(sql, B.accountId);
    check(bAfter.preferences === 0, "B: preferences cascaded to 0", JSON.stringify(bAfter));
    check(bAfter.companies === 0, "B: user_companies cascaded to 0", JSON.stringify(bAfter));
    check(
      bAfter.notifications === 0,
      "B: user_job_notifications cascaded to 0",
      JSON.stringify(bAfter),
    );

    // ---------------------------------------------------------------------
    console.log("\n§6  the real accounts, unchanged");
    for (const row of real) {
      const now = await countsFor(sql, row.id);
      const was = before.get(row.id)!;
      check(
        await accountExists(sql, row.id),
        `${row.id}: the account still exists`,
      );
      check(
        now.preferences === was.preferences &&
          now.companies === was.companies &&
          now.notifications === was.notifications,
        `${row.id}: every count identical to §1 ` +
          `(preferences=${was.preferences} companies=${was.companies} notifications=${was.notifications})`,
        `now ${JSON.stringify(now)}`,
      );
      if (row.auth_user_id) {
        check(
          await authUserExists(sql, row.auth_user_id),
          `${row.id}: its Supabase identity still exists`,
        );
      }
    }
  } finally {
    // Unconditional, and it does not depend on the assertions passing, on them
    // failing, or on the work reaching the end — the same rule badge-check's
    // rollback follows. Everything here is deleted by fixed id, so this is a
    // no-op on a clean run and a repair on any other.
    const { sql } = await loadLib();
    await scrub(sql);
    const leftover: string[] = [];
    for (const who of THROWAWAYS) {
      if (await accountExists(sql, who.accountId)) leftover.push(`public.users ${who.accountId}`);
      if (await authUserExists(sql, who.authId)) leftover.push(`auth.users ${who.authId}`);
    }
    console.log(
      leftover.length === 0
        ? "\ncleanup: no throwaway rows remain"
        : `\ncleanup: FAILED, still present — ${leftover.join(", ")}`,
    );
    if (leftover.length > 0) failures += 1;
    await sql.end();
  }

  console.log(failures === 0 ? "\nAll checks passed.\n" : `\n${failures} check(s) FAILED.\n`);
  process.exit(failures === 0 ? 0 : 1);
}

main().catch((err) => {
  console.error("\ndelete-check failed to run:", err);
  process.exit(1);
});
