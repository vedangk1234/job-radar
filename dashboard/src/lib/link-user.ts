import type { User } from "@supabase/supabase-js";

import { sql } from "./db";
import { DEFAULT_PREFERENCES } from "./labels";

/**
 * Supabase identity -> public.users row. The one place the two tables meet.
 *
 * A Google sign-in creates a row in auth.users. Everything this project owns —
 * the filter, the watch list, seven thousand notification receipts — hangs off
 * public.users by foreign key. This resolves one to the other, and it is the
 * single highest-risk function in Phase 2a, because getting it wrong does not
 * throw: it silently creates a SECOND account for a person who already has one,
 * and their dashboard comes up empty with all their data still sitting in rows
 * nothing will ever join to again.
 *
 * THREE BRANCHES, IN ONE TRANSACTION:
 *
 *   1. auth_user_id already matches -> return that id. The steady state. Every
 *      request after the first takes this path, one indexed lookup, and it is
 *      what guarantees branch 2 runs exactly once in the lifetime of an account.
 *
 *   2. lower(email) matches an existing row -> claim it: set auth_user_id and
 *      return the id that row already had. THIS IS THE BRANCH THAT SAVES THE
 *      PHASE-1 ACCOUNT. That row's id came from gen_random_uuid() in 1a and
 *      cannot equal any auth uid; matching on email is the only thing that can
 *      connect the person signing in to the data they already own.
 *
 *   3. neither -> a genuinely new account. INSERT with id = the auth uid, so
 *      every account created from here on has public.users.id = auth.users.id
 *      and the Phase-1 row is the only asymmetric one.
 *
 * WHY THIS IS NOT A TRIGGER ON auth.users. The tutorial pattern would run
 * inside GoTrue's own signup transaction, where branch 2 cannot exist: an
 * INSERT of (auth uid, existing email) violates users_email_lower_idx, the
 * raise aborts the signup, and the owner of the only real account is told
 * "Database error saving new user" and cannot get in at all. Running here also
 * means an auth.users row that somehow has no public.users row is repaired by
 * the next request rather than being a permanent orphan, because this runs on
 * every authenticated request and not once at signup.
 *
 * IDEMPOTENT BY CONSTRUCTION, not by being called carefully. Branch 1
 * short-circuits, branch 2's UPDATE is a no-op if re-run, and branch 3's INSERT
 * carries ON CONFLICT DO NOTHING with a re-select behind it.
 */

export type LinkedUser = {
  id: string;
  email: string;
  /** True only on the request that created the row. Nothing branches on it; it is for logs. */
  created: boolean;
};

/**
 * Does Google actually vouch for this address?
 *
 * Branch 2 hands over an existing account — with its watch list and its history
 * — to whoever presents a verified claim to that email. So the verification is
 * not a formality, it is the entire security boundary of the adoption, and it
 * is checked before the link rather than trusted afterwards. Phase 1's Auth.js
 * config refused `profile.email_verified === false` for the same reason; this
 * is that rule carried across.
 *
 * Three sources, because the claim's location is a property of the provider and
 * of the SDK version rather than something to hardcode: the Google identity's
 * own identity_data, the aggregated user_metadata, and GoTrue's own
 * email_confirmed_at. An explicit false anywhere refuses outright. Absent
 * everywhere is ALSO a refusal — the default is closed, so a provider that
 * simply does not say cannot be read as a yes.
 */
export type VerificationSource =
  | "identity_data"
  | "user_metadata"
  | "email_confirmed_at"
  | "none";

/**
 * The verdict AND which of the three sources produced it.
 *
 * One implementation, two callers, because the alternative is a second copy of
 * the precedence written for logging that drifts from the one that decides —
 * and then the log confidently names a source that had no say. The predicate
 * below is the only thing the request path calls; `source` exists so the answer
 * to "which claim path actually populated" is something the server OBSERVES
 * rather than something a person infers from what GoTrue happens to store.
 */
export function checkEmailVerified(user: User): { verified: boolean; source: VerificationSource } {
  const identity = user.identities?.find((i) => i.provider === "google");
  const fromIdentity = identity?.identity_data?.email_verified;
  const fromMetadata = user.user_metadata?.email_verified;

  if (fromIdentity === false || fromMetadata === false) {
    return { verified: false, source: fromIdentity === false ? "identity_data" : "user_metadata" };
  }
  if (fromIdentity === true) return { verified: true, source: "identity_data" };
  if (fromMetadata === true) return { verified: true, source: "user_metadata" };
  return user.email_confirmed_at
    ? { verified: true, source: "email_confirmed_at" }
    : { verified: false, source: "none" };
}

export function emailIsVerified(user: User): boolean {
  return checkEmailVerified(user).verified;
}

export class UnverifiedEmailError extends Error {
  constructor(email: string) {
    super(
      `Google did not confirm ${email} as a verified address, so this sign-in ` +
        `cannot be linked to an account. An unverified address may belong to ` +
        `someone else.`,
    );
    this.name = "UnverifiedEmailError";
  }
}

export async function linkUser(user: User): Promise<LinkedUser> {
  const email = (user.email ?? "").trim().toLowerCase();
  if (!email) {
    throw new Error("the authenticated user has no email address; cannot link an account");
  }
  if (!emailIsVerified(user)) {
    throw new UnverifiedEmailError(email);
  }

  // Which claim path decided, and which identities the session actually carried.
  // Reaching this line at all is the proof the check ran: an unverified address
  // throws above, before any statement.
  const verification = checkEmailVerified(user);
  console.log(
    `[auth/link] email_verified=${verification.verified} decided by ${verification.source}; ` +
      `identities on the session user: ${user.identities?.map((i) => i.provider).join(",") || "NONE"}`,
  );

  return sql.begin(async (tx) => {
    // --- 1. already linked -------------------------------------------------
    const [linked] = await tx<{ id: string; email: string }[]>`
      SELECT id, email FROM public.users WHERE auth_user_id = ${user.id}
    `;
    if (linked) {
      console.log(`[auth/link] branch 1 (already linked) -> public.users.id ${linked.id}`);
      return { id: linked.id, email: linked.email, created: false };
    }

    // --- 2. adopt the existing row by email --------------------------------
    // lower() on both sides, matching users_email_lower_idx — the same
    // comparison db/users.py and the old currentUser.ts bridge both make, so
    // an address differing only in case still finds its owner rather than
    // quietly creating a duplicate that no index would have caught either.
    const [adopted] = await tx<{ id: string; email: string }[]>`
      UPDATE public.users
         SET auth_user_id = ${user.id}
       WHERE lower(email) = lower(${email})
         AND auth_user_id IS NULL
      RETURNING id, email
    `;
    if (adopted) {
      console.log(
        `[auth/link] branch 2 (ADOPTED the existing row by email) -> public.users.id ${adopted.id}, ` +
          `auth_user_id ${user.id}`,
      );
      return { id: adopted.id, email: adopted.email, created: false };
    }

    // --- 3. a genuinely new account ----------------------------------------
    // id = the auth uid, so every account from here on satisfies
    // public.users.id = auth.users.id.
    //
    // is_active FALSE, deliberately, and it is honest rather than restrictive:
    // notify/digest.assert_self_send refuses any recipient other than MY_EMAIL
    // while FROM_EMAIL is still Resend's sandbox sender, so a new account with
    // is_active TRUE would enter the hourly fan-out, fail, and turn the cron's
    // "Send the digests" step permanently red for everyone. 2d verifies a
    // sending domain; that is the sub-phase that can flip this to TRUE.
    //
    // ON CONFLICT DO NOTHING covers two concurrent first sign-ins racing here.
    // The loser inserts nothing and falls through to the re-select below, which
    // is why that re-select is not defensive padding — it is the other half of
    // the race.
    const [created] = await tx<{ id: string; email: string }[]>`
      INSERT INTO public.users (id, email, auth_user_id, is_active)
      VALUES (${user.id}, ${email}, ${user.id}, FALSE)
      ON CONFLICT DO NOTHING
      RETURNING id, email
    `;

    if (created) {
      // A preferences row in the same transaction. Without one, loadPreferences
      // throws and Home, /jobs and /preferences all 500 for every new account —
      // the row is not a nicety, it is the difference between an account that
      // works and an account that errors on its first page load.
      //
      // Seeded from lib/labels.DEFAULT_PREFERENCES, which mirrors
      // notify/match.py's DEFAULT_* constants, so a new user's filter is the
      // same filter a fresh install has always started with.
      await tx`
        INSERT INTO preferences (user_id, cities, keywords, exclusions, max_level)
        VALUES (${created.id},
                ${DEFAULT_PREFERENCES.cities}::text[],
                ${DEFAULT_PREFERENCES.keywords}::text[],
                ${DEFAULT_PREFERENCES.exclusions}::text[],
                ${DEFAULT_PREFERENCES.max_level})
        ON CONFLICT DO NOTHING
      `;
      console.log(
        `[auth/link] branch 3 (NEW account) -> public.users.id ${created.id} (= the auth uid), is_active FALSE`,
      );
      return { id: created.id, email: created.email, created: true };
    }

    // Lost the race, or the email was taken between branch 2 and here. Re-read.
    const [existing] = await tx<{ id: string; email: string }[]>`
      SELECT id, email FROM public.users
       WHERE auth_user_id = ${user.id} OR lower(email) = lower(${email})
       LIMIT 1
    `;
    if (existing) return { id: existing.id, email: existing.email, created: false };

    throw new Error(
      `could not link Supabase user ${user.id} to a public.users row: the insert ` +
        `conflicted but no matching row could be read back`,
    );
  });
}
