import { sql } from "./db";
import type { ResumeStorage } from "./resume";

/**
 * Erasure. Both records for one person, in one transaction — and, since Phase 3
 * Slice 1, one file that is in neither of them.
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
 *
 * THE THIRD THING, AND THE ONLY ONE NO TRANSACTION CAN HOLD. Slice 1 stores a
 * resume as an object in a private Supabase Storage bucket. `resumes` is an
 * ordinary table and cascades off public.users like the other three; the FILE
 * does not, and no foreign key can make it. So erasure grew a step, and the
 * ORDER of that step is the entire decision:
 *
 *     1. list the person's storage folder, remove everything in it, list AGAIN
 *        to confirm it is empty. If anything survives -> throw, and RETURN
 *        BEFORE THE TRANSACTION OPENS. Nothing is deleted. The account is intact
 *        and the person is told.
 *     2. only then, the transaction below, unchanged except that `resumes` joins
 *        the cascade verification.
 *
 * WHY THE FILE GOES FIRST, WHICH IS THE OPPOSITE OF THE OBVIOUS ORDER. The
 * obvious order is to delete the rows and then tidy up the object. Do that and
 * the tidy-up runs after auth.users is gone — which means after the JWT this
 * request is holding has become worthless. There is no service-role key in this
 * app, so at that moment NOTHING IN THE SYSTEM CAN DELETE THAT OBJECT ANY MORE:
 * the only credential that ever could was the identity we just erased. A
 * failure there is a person's CV sitting in a bucket permanently, with the
 * account that could have reached it deleted, and a success page on screen.
 *
 * File-first inverts which failure is possible. The worst case becomes: the
 * resume is gone, the account is not, and an error says so — recoverable by
 * uploading again, and honest. That is a strictly better bad day, and it is the
 * only ordering under which "we told you it was erased" is never a lie.
 *
 * IT LISTS THE FOLDER RATHER THAN READING resumes.bucket_path. The column names
 * the CURRENT object; a replace that changed file type and then half-failed
 * could have left an earlier one beside it. Erasure is the wrong place to trust
 * a pointer when the directory itself can be enumerated — and the storage policy
 * scopes that listing to the person's own folder, so it cannot enumerate anybody
 * else's.
 */

/** What was actually removed. Logged, and asserted by scripts/delete-check.ts. */
export type DeletionReceipt = {
  preferences: number;
  userCompanies: number;
  userJobNotifications: number;
  /** The `resumes` row: 1 if they had uploaded one. */
  resumes: number;
  /** Objects removed from the bucket, counted before the transaction opened. */
  storageObjects: number;
  /** 1 normally. 0 is impossible — the function throws first. */
  publicUsers: number;
  /** 1 normally; 0 if the identity was already gone. See below. */
  authUsers: number;
};

/** Row counts a person is about to lose, for the confirmation screen. */
export type AccountFootprint = {
  companies: number;
  notifications: number;
  /** Whether there is a file as well as rows. A boolean, because "1 resume" is not a count. */
  hasResume: boolean;
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

/**
 * The file survived, so the account stays.
 *
 * Deliberately NOT a warning logged beside a successful deletion. Erasure is a
 * legal promise as much as a technical one, and a half-erasure that reports
 * success is the one outcome this function may never produce — so this is thrown
 * before anything is deleted, and app/actions.ts turns it into a message on the
 * preferences page rather than a redirect to "your account is gone".
 */
export class ResumeErasureFailedError extends Error {
  constructor(detail: string) {
    super(
      `your account has NOT been deleted, because your resume file could not be ` +
        `removed first (${detail}). Nothing was erased. This is deliberate: the ` +
        `file can only be deleted while you are signed in, so deleting the ` +
        `account first would leave it stored with no way to remove it. Please try ` +
        `again.`,
    );
    this.name = "ResumeErasureFailedError";
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
  const [row] = await sql<{ companies: number; notifications: number; resumes: number }[]>`
    SELECT
      (SELECT count(*)::int FROM user_companies         WHERE user_id = ${userId}) AS companies,
      (SELECT count(*)::int FROM user_job_notifications WHERE user_id = ${userId}) AS notifications,
      (SELECT count(*)::int FROM resumes                WHERE user_id = ${userId}) AS resumes
  `;
  return {
    companies: row?.companies ?? 0,
    notifications: row?.notifications ?? 0,
    hasResume: (row?.resumes ?? 0) > 0,
  };
}

export async function deleteAccount(
  userId: string,
  authUserId: string,
  storage: ResumeStorage,
): Promise<DeletionReceipt> {
  // --- the file, first and outside the transaction ------------------------
  //
  // Outside because it is not a database operation and cannot be rolled back by
  // one — wrapping it in sql.begin() would only make the transaction longer
  // while buying no atomicity at all. First because after the transaction the
  // credential that can delete it no longer exists. See the header.
  const storageObjects = await eraseResumeObjects(authUserId, storage);

  return sql.begin(async (tx) => {
    // Counted BEFORE, because ON DELETE CASCADE reports nothing. A cascade is
    // silent by design: the DELETE below returns one row whether it took three
    // other rows with it or seven thousand, so the only way this function can
    // say what it removed is to look first.
    const [before] = await tx<
      { preferences: number; companies: number; notifications: number; resumes: number }[]
    >`
      SELECT
        (SELECT count(*)::int FROM preferences             WHERE user_id = ${userId}) AS preferences,
        (SELECT count(*)::int FROM user_companies          WHERE user_id = ${userId}) AS companies,
        (SELECT count(*)::int FROM user_job_notifications  WHERE user_id = ${userId}) AS notifications,
        (SELECT count(*)::int FROM resumes                 WHERE user_id = ${userId}) AS resumes
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
      { preferences: number; companies: number; notifications: number; resumes: number }[]
    >`
      SELECT
        (SELECT count(*)::int FROM preferences             WHERE user_id = ${userId}) AS preferences,
        (SELECT count(*)::int FROM user_companies          WHERE user_id = ${userId}) AS companies,
        (SELECT count(*)::int FROM user_job_notifications  WHERE user_id = ${userId}) AS notifications,
        (SELECT count(*)::int FROM resumes                 WHERE user_id = ${userId}) AS resumes
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
      resumes: before?.resumes ?? 0,
      storageObjects,
      publicUsers: deleted.length,
      authUsers: authDeleted.length,
    };
  });
}

/**
 * Remove every object in this person's storage folder, and prove it is empty.
 *
 * RE-LISTED RATHER THAN TRUSTED, which is the same argument the cascade check
 * inside the transaction makes one layer down. Supabase's remove() reports an
 * error when the call fails; it does not promise that a call which returned no
 * error removed everything the caller asked for. Listing again costs one request
 * and converts "the API did not complain" into "the folder is empty", and those
 * are different claims — only the second is the one being made to a person
 * exercising a right to erasure.
 *
 * AN EMPTY FOLDER IS A NORMAL, SILENT SUCCESS. Most accounts have no resume, so
 * this is usually one list call returning nothing and no removal at all. It is
 * NOT an error and must never be: refusing to delete an account because it had
 * no file to delete would be the erasure right defeated by a missing optional
 * feature.
 *
 * A FAILURE TO LIST IS A FAILURE, not a shrug. If storage cannot be reached we
 * do not know whether there is a file, and "we could not check" is not a basis
 * for telling somebody their data is gone. It throws, and the account survives.
 */
async function eraseResumeObjects(
  authUserId: string,
  storage: ResumeStorage,
): Promise<number> {
  let paths: string[];
  try {
    paths = await storage.list(authUserId);
  } catch (err) {
    throw new ResumeErasureFailedError(
      `the storage folder could not be read, so it is not known whether a file is ` +
        `there: ${err instanceof Error ? err.message : String(err)}`,
    );
  }

  if (paths.length === 0) return 0;

  const failure = await storage.remove(paths);
  if (failure) throw new ResumeErasureFailedError(failure);

  const remaining = await storage.list(authUserId).catch(() => {
    // The removal reported success and the confirmation could not run. That is
    // not good enough to proceed on: the whole reason for re-listing is that the
    // first answer is not evidence.
    throw new ResumeErasureFailedError(
      "the files were removed but the folder could not be re-read to confirm it",
    );
  });

  if (remaining.length > 0) {
    throw new ResumeErasureFailedError(
      `${remaining.length} file(s) are still in the folder after being removed: ` +
        remaining.join(", "),
    );
  }

  return paths.length;
}
