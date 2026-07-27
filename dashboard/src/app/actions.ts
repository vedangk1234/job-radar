"use server";

import { signIn, signOut } from "@/auth";

/**
 * Sign in / out as server actions rather than via next-auth/react.
 *
 * The client helpers would work, but they pull in a SessionProvider and a
 * client-side session fetch for two buttons that do nothing else. A form posting
 * to a server action needs neither, and it keeps working with JavaScript
 * disabled — which is a fair description of the first paint on a phone with a
 * bad connection, the exact situation this dashboard gets opened in.
 */

export async function signInWithGoogle(formData: FormData) {
  const next = String(formData.get("next") ?? "/");
  // Path-only, so a crafted ?next= cannot bounce the sign-in through to
  // another origin. "//evil.com" is a protocol-relative URL, hence the second
  // check rather than just startsWith("/").
  const redirectTo = next.startsWith("/") && !next.startsWith("//") ? next : "/";
  await signIn("google", { redirectTo });
}

export async function signOutEverywhere() {
  await signOut({ redirectTo: "/signin" });
}
