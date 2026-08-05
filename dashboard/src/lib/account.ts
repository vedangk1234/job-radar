import { sql } from "./db";

/**
 * Erasure. Both records for one person, in one transaction.
 *
 * WHY THERE ARE TWO RECORDS AND WHY DELETING ONE IS WORSE THAN DELETING NEITHER
 * ----------------------------------------------------------------------------
 * Since 2a a person exists twice: `auth.users` is the identity Google signs in
 * to, and `public.users` is the account everything this project owns hangs off
 * by foreign key. Remove only the first and the app data survives with nothing
 * that can ever reach it again — including the email address, which is the one
 * column erasure is actually about. Remove only the second and the next request
 * from that still-valid session walks into lib/link-user.ts branch 3, which
 * dutifully creates a fresh account. Half a delete is not most of a delete; it
 * is a different, worse outcome than not starting.
 *
 * THE OBVIOUS SHAPE, AND WHY THIS IS NOT IT. The documented way to remove an
 * auth user is supabase.auth.admin.deleteUser(), which needs the service-role
 * key. This app deliberately has no such key — lib/env.ts and
 * lib/supabase/server.ts both say so, and the anon key can be published
 * precisely because the RLS block in db/schema.sql leaves it holding nothing.
 * Putting an all-powerful credential on Vercel to delete one row a month would
 * undo that, and it would buy a WORSE guarantee than what is written below: a
 * pooler DELETE followed by an HTTPS admin call is two operations that can fail
 * apart, which is exactly the orphan this function exists to make impossible.
 *
 * WHAT IT DOES INSTEAD. DASHBOARD_DB_URL connects as `postgres`, which on this
 * project holds DELETE on auth.users — the same privilege the Supabase SQL
 * editor runs a manual `delete from auth.users` with. So both rows are removed
 * over the connection every other read and write in this app already uses, in
 * ONE transaction. There is no window between them. Either the person is gone
 * from both tables or nothing happened at all, and no code path has to reason
 * about a half-deleted account because none can produce one.
 *
 * THE TWO IDS ARE NOT INTERCHANGEABLE, and on this database that is not
 * theoretical. Every account created since 2a satisfies
 * public.users.id = auth.users.id, but the Phase-1 account predates auth: its id
 * came from gen_random_uuid() in 1a and its auth_user_id was filled in by
 * link-user branch 2 when its owner first signed in with Google. The two values
 * differ for that row and only for that row. A `DELETE FROM auth.users WHERE id
 * = <public.users.id>` therefore deletes nothing at all for the one account that
 * holds fifty companies and seven thousand notification rows — it would report
 * success and leave the identity behind. The auth uid is passed in separately,
 * from the verified session, and is the only thing the auth delete matches on.
 *
 * NOTHING HERE TAKES AN ID FROM A REQUEST. Both arguments come from lib/guard,
 * which reads them off a JWT verified against the auth server. See
 * app/actions.ts.
 */

/** What was actually removed. Logged, and asserted by scripts/delete-check.ts. */
export type DeletionReceipt = {
  preferences: number;
  userCompanies: number;
  userJobNotifications: number;
  /** 1 normally. 0 is impossible — the function throws first. */
  publicUsers: number;
  /** 1 normally; 0 if the identity was already gone. See below. */
  authUsers: number;
};

/** Row counts a person is about to lose, for the confirmation screen. */
export type AccountFootprint = {
  companies: number;
  notifications: number;
};

export class AccountNotFoundError extends Error {
  constructor(userId: string) {
    super(
      `no public.users row matched id ${userId} together with the auth identity ` +
        `on this session; nothing was deleted`,
    );
    this.name = "AccountNotFoundError";
  }
}

export class CascadeFailedError extends Error {
  constructor(detail: string) {
    super(
      `the account row was deleted but per-user rows survived it (${detail}). ` +
        `An ON DELETE CASCADE is missing from db/schema.sql. The transaction ` +
        `has been rolled back and nothing was deleted.`,
    );
    this.name = "CascadeFailedError";
  }
}

/**
 * How much data this account holds, so the confirmation can say so.
 *
 * "This deletes everything" is a sentence nobody reads. "This deletes 50 tracked
 * companies and 7,708 notification records" is one they do, and under the DPDP
 * Act the person consenting to erasure is entitled to know what is going.
 */
export async function accountFootprint(userId: string): Promise<AccountFootprint> {
  const [row] = await sql<{ companies: number; notifications: number }[]>`
    SELECT
      (SELECT count(*)::int FROM user_companies         WHERE user_id = ${userId}) AS companies,
      (SELECT count(*)::int FROM user_job_notifications WHERE user_id = ${userId}) AS notifications
  `;
  return { companies: row?.companies ?? 0, notifications: row?.notifications ?? 0 };
}

export async function deleteAccount(userId: string, authUserId: string): Promise<DeletionReceipt> {
  return sql.begin(async (tx) => {
    // Counted BEFORE, because ON DELETE CASCADE reports nothing. A cascade is
    // silent by design: the DELETE below returns one row whether it took three
    // other rows with it or seven thousand, so the only way this function can
    // say what it removed is to look first.
    const [before] = await tx<
      { preferences: number; companies: number; notifications: number }[]
    >`
      SELECT
        (SELECT count(*)::int FROM preferences             WHERE user_id = ${userId}) AS preferences,
        (SELECT count(*)::int FROM user_companies          WHERE user_id = ${userId}) AS companies,
        (SELECT count(*)::int FROM user_job_notifications  WHERE user_id = ${userId}) AS notifications
    `;

    // BOTH COLUMNS IN THE PREDICATE, and the second one is the security clause
    // rather than a belt-and-braces flourish. It is what ties the row being
    // deleted to the identity that was actually authenticated: a userId alone
    // would delete whatever account carried it, and this is the single most
    // destructive statement in the app.
    const deleted = await tx<{ id: string; email: string }[]>`
      DELETE FROM public.users
       WHERE id = ${userId}
         AND auth_user_id = ${authUserId}
      RETURNING id, email
    `;
    if (deleted.length === 0) throw new AccountNotFoundError(userId);

    // THE CASCADE, VERIFIED RATHER THAN TRUSTED, INSIDE THE TRANSACTION THAT CAN
    // STILL TAKE IT BACK. All three foreign keys are declared ON DELETE CASCADE
    // in db/schema.sql and all three were confirmed against the live database —
    // but this is erasure, the promise is legal as well as technical, and a
    // future ALTER TABLE that re-adds one of these keys without the clause would
    // leave a person's watch list and notification history sitting behind a
    // deleted account with no symptom anywhere. Reading the counts back costs one
    // statement and converts that silent outcome into a rollback and an error.
    const [after] = await tx<
      { preferences: number; companies: number; notifications: number }[]
    >`
      SELECT
        (SELECT count(*)::int FROM preferences             WHERE user_id = ${userId}) AS preferences,
        (SELECT count(*)::int FROM user_companies          WHERE user_id = ${userId}) AS companies,
        (SELECT count(*)::int FROM user_job_notifications  WHERE user_id = ${userId}) AS notifications
    `;
    const survivors = Object.entries(after ?? {}).filter(([, n]) => n > 0);
    if (survivors.length > 0) {
      throw new CascadeFailedError(survivors.map(([k, n]) => `${k}=${n}`).join(", "));
    }

    // The identity, matched on the auth uid and never on userId — see the header.
    // auth.identities, auth.sessions and auth.refresh_tokens all cascade off this
    // row (verified against the live catalogue), so this one statement is the
    // whole of the Supabase-side removal.
    //
    // ZERO ROWS IS TOLERATED, and only this case is. A missing privilege raises
    // 42501 and takes the whole transaction with it, so reaching this line with
    // no row deleted means one thing: the identity was already gone — removed
    // from the Supabase console, say — while public.users still pointed at it.
    // Refusing here would make that stale pointer permanent and leave a person
    // unable to erase data they still own, which is the opposite of the right
    // this implements.
    const authDeleted = await tx<{ id: string }[]>`
      DELETE FROM auth.users WHERE id = ${authUserId} RETURNING id
    `;
    if (authDeleted.length === 0) {
      console.warn(
        `[account/delete] public.users ${userId} pointed at auth user ${authUserId}, ` +
          `which no longer existed. The app account has been deleted.`,
      );
    }

    return {
      preferences: before?.preferences ?? 0,
      userCompanies: before?.companies ?? 0,
      userJobNotifications: before?.notifications ?? 0,
      publicUsers: deleted.length,
      authUsers: authDeleted.length,
    };
  });
}
