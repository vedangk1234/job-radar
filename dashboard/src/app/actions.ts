"use server";

import { redirect } from "next/navigation";

import { deleteAccount, ResumeErasureFailedError } from "@/lib/account";
import { requirePage } from "@/lib/guard";
import { resumeStorage } from "@/lib/resume";
import { createClient } from "@/lib/supabase/server";

/**
 * Sign OUT as a server action, rather than via a client SDK.
 *
 * The browser helpers would work, but they pull a client-side Supabase instance
 * and a session listener into the bundle for a button that does nothing else. A
 * form posting to a server action needs neither, and it keeps working with
 * JavaScript disabled — which is a fair description of the first paint on a
 * phone with a bad connection, the exact situation this dashboard gets opened
 * in. That argument is unchanged from Phase 1; only the library underneath it
 * changed.
 *
 * SIGN-IN USED TO LIVE HERE AND COULD NOT. The two directions look symmetrical
 * and are not: signing out redirects to a path in this app, signing in has to
 * hand the browser to accounts.google.com. redirect() from a Server Action is a
 * client-router navigation, and for an external URL it bounced back to /signin
 * without ever reaching the provider — so no code was ever issued, and the PKCE
 * verifier the same call had just written correctly was never redeemed. It is a
 * Route Handler now: app/auth/signin/route.ts. Sign-out stays here precisely
 * because its destination is internal.
 */

export async function signOutEverywhere() {
  const supabase = await createClient();
  await supabase.auth.signOut();
  redirect("/signin");
}

/**
 * Delete this account and everything hanging off it. The DPDP right to erasure.
 *
 * WHOSE ACCOUNT: THE SESSION'S, AND THERE IS NO SECOND ANSWER. Both ids come
 * from requirePage(), which reads them off a JWT verified against the auth
 * server by getUser(). The form posts exactly one field and it is the typed
 * confirmation — no user id, no email, nothing an id could be read out of. This
 * is the same rule every route learned in 2a, and it matters more here than
 * anywhere else in the app: everywhere else a trusted id decides what you can
 * READ, and here it decides whose seven thousand rows stop existing.
 *
 * THE CONFIRMATION IS CHECKED ON THE SERVER, which is the only place a
 * confirmation is a confirmation. A window.confirm() or a disabled button is a
 * prompt for a person who is about to make a mistake and no obstacle at all to a
 * POST, and — the reason that argument even applies here — this dashboard is
 * built to work with JavaScript off, so a client-side guard would be absent
 * exactly when the form still submits. Typing your own address is deliberately
 * more work than clicking: 2a opened sign-up, so the person reaching this is a
 * stranger three minutes into the product who should not be able to erase it by
 * clicking one thing twice.
 *
 * WHY THE DELETE COMES BEFORE THE SIGN-OUT. Signing out first would drop the
 * session, and the very next request would be redirected to /signin with the
 * account still there — a person who asked to be deleted, told nothing, and
 * still in the database. Deleting first makes the sign-out an optimisation
 * rather than a step that can fail: the auth row is already gone, so every gate
 * in the app calls getUser(), gets null, and bounces to /signin whatever happens
 * to the cookies. It is still attempted, and its failure is still logged, so the
 * browser is not left carrying credentials for somebody who no longer exists.
 *
 * NOT WRAPPED IN try/catch AROUND redirect(): redirect() works by throwing, and
 * catching it here would turn a completed deletion into a 500 on a page whose
 * account no longer exists. Only the sign-out is guarded, and only because it is
 * the one call expected to fail.
 *
 * SINCE SLICE 1 THERE IS ONE MORE THING THAT CAN STOP THE DELETE, and it stops
 * it completely rather than partially. deleteAccount() removes the resume FILE
 * before it opens the transaction, and refuses to go on if the file survives —
 * because after the transaction the JWT this request holds is dead and no
 * credential in this app can reach that object again. So a
 * ResumeErasureFailedError means nothing at all was deleted, and it is reported
 * on the preferences page with the account still there. The one thing this
 * action may never do is send somebody to "your account is gone" over a file
 * that is still stored.
 */
export async function deleteAccountAction(formData: FormData) {
  const { userId, authUserId, email } = await requirePage();

  const typed = String(formData.get("confirm") ?? "").trim().toLowerCase();
  if (typed !== email.trim().toLowerCase()) {
    // lower() on both sides, the same comparison lib/link-user.ts makes against
    // users_email_lower_idx — a person who types their own address with a
    // capital letter has confirmed, and telling them otherwise is theatre.
    console.warn(`[account/delete] refused: the typed confirmation did not match the session address`);
    redirect("/preferences?delete=mismatch");
  }

  // The storage adapter is built HERE and passed down, so lib/account.ts stays a
  // database module that imports no Supabase and no next/headers. It carries
  // this session's own JWT, which is the only credential in the app that can
  // delete this person's resume — see the ordering argument in lib/account.ts.
  const storage = resumeStorage(await createClient());

  let receipt;
  try {
    receipt = await deleteAccount(userId, authUserId, storage);
  } catch (err) {
    if (err instanceof ResumeErasureFailedError) {
      // NOT a 500 and emphatically not a redirect to "your account is gone".
      // Nothing was deleted, the person is still signed in, and the page they
      // came from is where the explanation belongs.
      console.error("[account/delete] refused: the resume file could not be erased", err);
      redirect("/preferences?delete=resume");
    }
    throw err;
  }

  console.log(
    `[account/delete] erased public.users ${userId} (auth ${authUserId}): ` +
      `preferences=${receipt.preferences} user_companies=${receipt.userCompanies} ` +
      `user_job_notifications=${receipt.userJobNotifications} resumes=${receipt.resumes} ` +
      `storage_objects=${receipt.storageObjects} auth.users=${receipt.authUsers}`,
  );

  try {
    const supabase = await createClient();
    await supabase.auth.signOut();
  } catch (err) {
    // Expected to be possible rather than expected: the auth user this session
    // names no longer exists, so GoTrue may refuse the revoke. The cookies are
    // already worthless — see the header.
    console.error("[account/delete] sign-out after deletion failed; the session is dead regardless", err);
  }

  redirect("/signin?deleted=1");
}
