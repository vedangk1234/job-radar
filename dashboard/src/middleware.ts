import { NextResponse, type NextRequest } from "next/server";

import { updateSession } from "@/lib/supabase/middleware";

/**
 * Nothing in this app is public.
 *
 * The matcher below is an allow-NOTHING list with four holes in it, rather than
 * a list of protected paths. That direction matters and is unchanged from Phase
 * 1: a new page added next month is protected by default, and forgetting to
 * register it cannot expose it. Only the holes moved — Auth.js's /api/auth/*
 * endpoints are gone and Supabase's code exchange at /auth/callback took their
 * place. It must be reachable while signed out or sign-in cannot complete.
 *
 * Pages redirect; API routes get a 401 with a JSON body, because a fetch() that
 * receives a 302 to an HTML login page reports a confusing parse error instead
 * of "you are logged out".
 *
 * THIS IS THE OUTER OF TWO GATES and it deliberately does less than the inner
 * one. It asks only "is there a verified user", never "which public.users row is
 * that" — the link runs in lib/guard.ts, on the handler side, where it can use a
 * database connection and where a failure can be reported properly. Middleware
 * runs on every asset-adjacent request in the matcher; putting a transaction
 * there would be a query per navigation to answer a question the handler is
 * about to ask again anyway.
 *
 * It also does the one thing only middleware can: refresh the Supabase session.
 * See lib/supabase/middleware.ts for why the response object it returns is the
 * one that has to be sent.
 */
export async function middleware(request: NextRequest) {
  const { response, user } = await updateSession(request);

  if (user) return response;

  const { pathname, search } = request.nextUrl;

  if (pathname.startsWith("/api/")) {
    return NextResponse.json({ error: "unauthorised", detail: "sign in at /signin" }, { status: 401 });
  }

  const signin = new URL("/signin", request.nextUrl.origin);
  // Where to land after a successful sign-in. Path-only, so a crafted ?next=
  // cannot turn the sign-in page into an open redirect to another site.
  // "//evil.com" is a protocol-relative URL, hence the second check.
  if (pathname !== "/" && !pathname.startsWith("//")) {
    signin.searchParams.set("next", pathname + search);
  }
  return NextResponse.redirect(signin);
}

export const config = {
  matcher: [
    /*
     * Everything except:
     *   auth/signin   — where sign-in STARTS. Gated, it would redirect the
     *                   sign-in attempt to /signin, which redirects here: the
     *                   gate would be locking the door it is standing behind.
     *   auth/callback — Supabase's code exchange, which must work while signed out
     *   signin        — the page you are sent to when this gate closes
     *   _next/*       — build output
     *   favicon.ico, *.svg/png/ico/webp/jpg/jpeg — static files
     *
     * THE IMAGE EXTENSIONS ARE NOT DECORATION. Until the landing page's hero
     * arrived they were svg/png/ico, and public/hero/ ships .webp and .jpg — so
     * every request for the backdrop matched this gate, found no session and was
     * answered with a 307 to /signin. The browser asked for a picture and was
     * handed the sign-in page's HTML, which renders as a blank black hero and
     * nothing in a server log that looks like an error. Extensions, not paths:
     * an allowlisted directory is where an unguarded route would eventually
     * appear, and every page and API route re-checks for itself in any case.
     *
     * The two auth/* holes are the whole of the sign-in flow and nothing else:
     * one issues the redirect to Google, one redeems the code it comes back
     * with. Neither reads this project's own data — see lib/guard.ts for the
     * gate that does.
     */
    "/((?!auth/signin|auth/callback|signin|_next/static|_next/image|favicon\\.ico|.*\\.(?:svg|png|ico|webp|jpg|jpeg)$).*)",
  ],
};
