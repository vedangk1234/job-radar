import { NextResponse } from "next/server";
import { redirect } from "next/navigation";

import { linkUser, UnverifiedEmailError } from "./link-user";
import { getAuthUser } from "./supabase/server";

/**
 * The gate, and since Phase 2a also the place identity is resolved.
 *
 * WHAT CHANGED IN 2a, because it is a change of meaning and not just of
 * library. Phase 1 asked one question — "is the signed-in address the single
 * ALLOWED_EMAIL?" — and answered a second one somewhere else entirely: whose
 * rows am I reading? That came from MY_EMAIL via lib/currentUser.ts, and nothing
 * checked the two named the same person. It was safe only because there was one
 * of each. With sign-up open, an authenticated stranger resolving to MY_EMAIL's
 * user id would be a stranger looking at the operator's dashboard, so the two
 * questions are now answered in one place, from one source: the session.
 *
 * requireSession() and requirePage() therefore return a userId as well as an
 * email, and it is a public.users.id — already linked, already adopted if this
 * person had a Phase-1 account. Callers thread it down explicitly rather than
 * reaching for an ambient lookup, which is what made 2b a subtraction rather
 * than a rewrite: there is no MY_EMAIL lookup under src/ any more at all — the
 * headless checks kept theirs in dashboard/scripts/current-user.ts — so THIS is
 * the only thing in the deployed app that answers "who is asking".
 *
 * STILL BELT AND BRACES WITH middleware.ts, for the reason it always was: the
 * middleware matcher is a regex, and a regex is one careless edit from a hole
 * nothing tests. Re-checking inside the handler keeps the guarantee local to the
 * code that touches the database — a route can be read in isolation and seen to
 * be protected. The linking is here rather than only in the OAuth callback for a
 * related reason: running on every authenticated request means an auth.users row
 * that somehow has no public.users row is repaired by the next request instead
 * of being a permanent orphan.
 */

export type Session = { userId: string; email: string };

/**
 * The session for a route handler, or a response to return instead.
 *
 * getAuthUser() is getUser(), never getSession() — the JWT is verified against
 * the auth server rather than decoded out of a cookie the network supplied.
 */
export async function requireSession(): Promise<
  { ok: true; session: Session } | { ok: false; response: NextResponse }
> {
  const user = await getAuthUser();
  if (!user) return { ok: false, response: unauthorised() };

  try {
    const linked = await linkUser(user);
    return { ok: true, session: { userId: linked.id, email: linked.email } };
  } catch (err) {
    // An unverified address is a 403 and not a 500: the request was understood,
    // authenticated, and refused. Anything else failed for an unknown reason and
    // is reported as one.
    if (err instanceof UnverifiedEmailError) {
      console.error("[auth] refused unverified email", err.message);
      return {
        ok: false,
        response: NextResponse.json(
          { error: "email_unverified", detail: err.message },
          { status: 403 },
        ),
      };
    }
    throw err;
  }
}

/**
 * The same gate for a page: redirect rather than 401, because a browser asking
 * for HTML should be sent somewhere it can sign in.
 */
export async function requirePage(): Promise<Session> {
  const user = await getAuthUser();
  if (!user) redirect("/signin");

  try {
    const linked = await linkUser(user);
    return { userId: linked.id, email: linked.email };
  } catch (err) {
    if (err instanceof UnverifiedEmailError) {
      console.error("[auth] refused unverified email", err.message);
      redirect("/signin?error=email_unverified");
    }
    throw err;
  }
}

function unauthorised(): NextResponse {
  return NextResponse.json({ error: "unauthorised", detail: "sign in at /signin" }, { status: 401 });
}

/** A 400 with a message meant for a human to read on screen. */
export function badRequest(detail: string): NextResponse {
  return NextResponse.json({ error: "bad_request", detail }, { status: 400 });
}

/**
 * A 500 that says nothing useful to the client and everything to the log.
 *
 * THIS USED TO RETURN err.message, AND THE ARGUMENT FOR THAT EXPIRED IN 2a. The
 * reasoning was explicit and, at the time, correct: there is exactly one
 * authorised user, they are the operator, and a generic "something went wrong"
 * would mean opening a terminal to discover that Supabase was asleep. Phase 2a
 * removes the allowlist, so the reader of an error is now any stranger who
 * signed up ninety seconds ago — and this project's error messages are candid
 * in a way that is a feature for an operator and reconnaissance for anyone else:
 * they name tables, columns, migration scripts and environment variables.
 *
 * So the detail goes to the server log, where the operator can still read it,
 * and the client gets a correlation id to quote. Same information, one audience.
 */
export function serverError(err: unknown, context: string): NextResponse {
  const ref = Math.random().toString(36).slice(2, 10);
  console.error(`[${context}] ref=${ref}`, err);
  return NextResponse.json(
    {
      error: "server_error",
      detail: "Something went wrong on the server. The details are in the server log.",
      ref,
    },
    { status: 500 },
  );
}
