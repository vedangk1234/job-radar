import { NextResponse } from "next/server";
import { redirect } from "next/navigation";

import { linkUser, UnverifiedEmailError } from "./link-user";
import { onboardingCompleted } from "./preferences";
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

/**
 * WHY THERE ARE TWO IDS HERE, ADDED IN 2c. `userId` is public.users.id — the
 * key every foreign key in this project points at, and the only one any page or
 * route needs. `authUserId` is auth.users.id, the Supabase identity, and exactly
 * one thing reads it: lib/account.deleteAccount, which has to remove the auth
 * row as well and cannot derive that id from this one. It cannot, because the
 * two are equal for every account created since 2a and NOT equal for the
 * Phase-1 account, whose id predates auth entirely. Carrying it on the session
 * means the delete matches the identity that was actually verified rather than
 * one looked up afterwards from a column that could have been written wrong.
 */
export type Session = { userId: string; email: string; authUserId: string };

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
    return { ok: true, session: { userId: linked.id, email: linked.email, authUserId: user.id } };
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

/** Where a signed-in account that has not chosen a filter yet is sent. */
export const ONBOARDING_PATH = "/onboarding";

/**
 * The same gate for a page: redirect rather than 401, because a browser asking
 * for HTML should be sent somewhere it can sign in.
 *
 * AND, SINCE SLICE 1b, THE SECOND GATE: a signed-in account whose
 * preferences.onboarding_completed is FALSE is sent to /onboarding instead of
 * the page it asked for. Same shape as the email-verify redirect above — a fact
 * read from the database on the handler side, answered with a redirect rather
 * than an error, because the person is authenticated and there is somewhere for
 * them to go.
 *
 * WHY IT LIVES IN requirePage() ITSELF AND NOT IN A requireOnboardedPage()
 * BESIDE IT. tests/test_dashboard_auth.test_every_page_guards_itself already
 * forces every page.tsx to call this function, so putting the gate here means a
 * page added next month is gated by DEFAULT and forgetting about onboarding
 * cannot open a hole. It is the same direction the middleware matcher chose for
 * the same reason: an allow-nothing list with named holes, never a list of the
 * things to protect.
 *
 * THE THREE HOLES, ENUMERATED HERE AND ASSERTED IN tests/test_dashboard_auth.py:
 *
 *   /onboarding — the redirect target. Gated, it would redirect to itself.
 *   /preferences — because DELETING AN ACCOUNT LIVES THERE. A person who signed
 *                  up and changed their mind must not have erasure held behind a
 *                  setup screen; that is a right, not a feature. It is not a way
 *                  round the gate either: the flag is flipped by a save, and a
 *                  save on that screen IS onboarding — the same editor writing
 *                  the same row through the same statement.
 *   deleteAccountAction — the same argument, for the form post itself, so the
 *                  delete can never be answered with "finish your setup first".
 *
 * NOT HERE: requireSession(). API routes are deliberately ungated, because
 * PUT /api/preferences is the ONLY exit from onboarding — gating the routes
 * would need a second exemption list whose first mistake seals the only door.
 * The data routes are already scoped to the caller's own user id, so an account
 * mid-onboarding can read nothing but its own empty answers.
 *
 * Sign-in and sign-out are untouched by construction rather than by exemption:
 * /signin and /auth/callback call no guard at all, and signOutEverywhere is a
 * server action that calls none either.
 */
export async function requirePage(
  { allowUnfinishedOnboarding = false }: { allowUnfinishedOnboarding?: boolean } = {},
): Promise<Session> {
  const user = await getAuthUser();
  if (!user) redirect("/signin");

  let session: Session;
  try {
    const linked = await linkUser(user);
    session = { userId: linked.id, email: linked.email, authUserId: user.id };
  } catch (err) {
    if (err instanceof UnverifiedEmailError) {
      console.error("[auth] refused unverified email", err.message);
      redirect("/signin?error=email_unverified");
    }
    throw err;
  }

  // AFTER the link and outside the try, both deliberately. After, because the
  // flag is keyed by public.users.id and that id is what linkUser exists to
  // produce. Outside, because redirect() works by throwing and a catch around it
  // would swallow the redirect and report it as an unverified email.
  if (!allowUnfinishedOnboarding && !(await onboardingCompleted(session.userId))) {
    redirect(ONBOARDING_PATH);
  }

  return session;
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
