import { NextResponse } from "next/server";
import { redirect } from "next/navigation";

import { auth, isAuthorised } from "@/auth";

/**
 * The inner gate. Every API route starts with this, even though middleware
 * already ran.
 *
 * Belt and braces by design: middleware protection is a regex matcher, and a
 * regex matcher is one careless edit away from a hole that nothing tests.
 * Re-checking inside the handler makes the guarantee local to the code that
 * touches the database — you can read a route in isolation and see that it is
 * protected, without reasoning about a pattern in another file.
 */
export async function requireSession(): Promise<
  { ok: true; email: string } | { ok: false; response: NextResponse }
> {
  const session = await auth();
  if (!isAuthorised(session)) {
    return {
      ok: false,
      response: NextResponse.json(
        { error: "unauthorised", detail: "sign in at /signin" },
        { status: 401 },
      ),
    };
  }
  return { ok: true, email: session!.user!.email! };
}

/**
 * The same gate for a page rather than a route: redirect instead of 401, because
 * a browser asking for HTML should be sent somewhere it can sign in.
 *
 * Same belt-and-braces argument as requireSession. The middleware matcher
 * already covers every page, but a page that queries the database on the server
 * should not depend on a regex in another file to decide whether it may. Under
 * normal operation this never fires — which is the point: it is the thing that
 * fires if the matcher ever develops a hole.
 */
export async function requirePage(): Promise<string> {
  const session = await auth();
  if (!isAuthorised(session)) redirect("/signin");
  return session!.user!.email!;
}

/** A 400 with a message meant for a human to read on screen. */
export function badRequest(detail: string): NextResponse {
  return NextResponse.json({ error: "bad_request", detail }, { status: 400 });
}

/**
 * A 500 that says what actually happened.
 *
 * The usual advice is to hide error detail from clients. It does not apply
 * here: there is exactly one authorised user, they are the operator, and a
 * generic "something went wrong" would mean opening a terminal to find out that
 * Supabase was asleep. The route is already behind the session gate.
 */
export function serverError(err: unknown, context: string): NextResponse {
  const detail = err instanceof Error ? err.message : String(err);
  console.error(`[${context}]`, err);
  return NextResponse.json({ error: "server_error", detail, context }, { status: 500 });
}
