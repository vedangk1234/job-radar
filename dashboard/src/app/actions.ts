"use server";

import { redirect } from "next/navigation";

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
