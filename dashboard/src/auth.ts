import NextAuth from "next-auth";
import Google from "next-auth/providers/google";

import { allowedEmail, googleClientId, googleClientSecret } from "@/lib/env";

/**
 * Google OAuth, locked to exactly one account.
 *
 * "Sign in with Google" authenticates ANY Google account — a billion of them.
 * Authentication is not authorisation, and the gap between the two is the whole
 * security model of this dashboard, which can toggle what the crawler watches
 * and edit the filter that decides what reaches an inbox. So the signIn
 * callback below is the actual lock: Google proves who you are, and then this
 * decides that the answer had better be ALLOWED_EMAIL.
 *
 * The check is enforced in three places, on purpose:
 *
 *   1. signIn — refuses to mint a session for the wrong account at all.
 *   2. jwt    — stamps the verdict into the token, so a session issued before
 *               ALLOWED_EMAIL changed does not outlive the change.
 *   3. isAuthorised — re-checked on every request by the middleware and by
 *               every API route, rather than trusting "a session exists".
 *
 * One of those would probably do. Three cost nothing and mean a mistake in any
 * single layer is not an open door.
 */

/**
 * env.ts throws on a missing value, which is right for a request handler and
 * wrong here: this module is evaluated at import time, including during
 * `next build`, which has no .env.local. So every read is softened to a
 * fallback — and every fallback is the closed one. A missing ALLOWED_EMAIL
 * authorises nobody; missing Google credentials make sign-in fail rather than
 * succeed. `missingConfig()` below is what turns that into a legible message on
 * the sign-in page instead of a silent, permanent redirect loop.
 */
function soft(read: () => string): string {
  try {
    return read();
  } catch {
    return "";
  }
}

function allowed(): string | null {
  return soft(allowedEmail) || null; // misconfigured -> nobody is allowed.
}

/** Which required environment variables are absent, for the sign-in page. */
export function missingConfig(): string[] {
  const missing: string[] = [];
  if (!soft(googleClientId)) missing.push("GOOGLE_CLIENT_ID");
  if (!soft(googleClientSecret)) missing.push("GOOGLE_CLIENT_SECRET");
  if (!soft(allowedEmail)) missing.push("ALLOWED_EMAIL");
  if (!(process.env.AUTH_SECRET ?? "").trim()) missing.push("AUTH_SECRET");
  return missing;
}

export const { handlers, auth, signIn, signOut } = NextAuth({
  providers: [
    Google({
      clientId: soft(googleClientId),
      clientSecret: soft(googleClientSecret),
      // Google's own account chooser, every time. Without it a browser already
      // signed into the wrong account silently retries the rejected one and the
      // sign-in page just blinks.
      authorization: { params: { prompt: "select_account" } },
    }),
  ],

  pages: {
    signIn: "/signin",
    error: "/signin",
  },

  session: { strategy: "jwt" },

  callbacks: {
    /**
     * The lock. Returning false here is what makes a successful Google login
     * still not a session.
     */
    signIn({ profile, user }) {
      const permitted = allowed();
      if (!permitted) return false;

      // Google says the address is unverified: it is not proof of identity, and
      // an unverified address can be a different person's.
      if (profile && profile.email_verified === false) return false;

      const email = String(profile?.email ?? user?.email ?? "")
        .trim()
        .toLowerCase();
      return !!email && email === permitted;
    },

    jwt({ token }) {
      const permitted = allowed();
      token.authorised = !!permitted && String(token.email ?? "").toLowerCase() === permitted;
      return token;
    },

    session({ session, token }) {
      session.user.authorised = token.authorised === true;
      return session;
    },
  },

  // AUTH_SECRET is read from the environment by Auth.js itself and is what
  // signs the session cookie. Without it every restart invalidates sessions in
  // dev and nothing works at all in production.
  trustHost: true,
});

/**
 * The one predicate the rest of the app asks. Deliberately not "is there a
 * session" — a session for the wrong address must read as unauthorised even if
 * it somehow exists.
 */
export function isAuthorised(session: { user?: { email?: string | null } } | null): boolean {
  const permitted = allowed();
  if (!permitted || !session?.user?.email) return false;
  return session.user.email.trim().toLowerCase() === permitted;
}
