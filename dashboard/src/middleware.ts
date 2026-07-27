import { NextResponse } from "next/server";

import { auth, isAuthorised } from "@/auth";

/**
 * Nothing in this app is public.
 *
 * The matcher below is an allow-NOTHING list with four holes in it, rather than
 * a list of protected paths. That direction matters: a new page added next
 * month is protected by default, and forgetting to register it cannot expose
 * it. The holes are the sign-in page, Auth.js's own endpoints (which must be
 * reachable while signed out or sign-in cannot happen), and static assets.
 *
 * Pages redirect; API routes get a 401 with a JSON body, because a fetch() that
 * receives a 302 to an HTML login page reports a confusing parse error instead
 * of "you are logged out".
 *
 * This is the OUTER of two gates. Every API route also calls requireSession()
 * for itself — see lib/guard.ts. Middleware is a matcher expression, and a
 * matcher expression is exactly the kind of thing that develops a hole.
 */
export default auth((req) => {
  if (isAuthorised(req.auth)) return NextResponse.next();

  const { pathname, search } = req.nextUrl;

  if (pathname.startsWith("/api/")) {
    return NextResponse.json(
      { error: "unauthorised", detail: "sign in at /signin" },
      { status: 401 },
    );
  }

  const signin = new URL("/signin", req.nextUrl.origin);
  // Where to land after a successful sign-in. Path-only, so a crafted ?next=
  // cannot turn the sign-in page into an open redirect to another site.
  if (pathname !== "/") signin.searchParams.set("next", pathname + search);
  return NextResponse.redirect(signin);
});

export const config = {
  matcher: [
    /*
     * Everything except:
     *   api/auth/*  — Auth.js's own routes (callback, csrf, signin, signout)
     *   signin      — the page you are sent to when this gate closes
     *   _next/*     — build output
     *   favicon.ico, *.svg/png/ico — static files
     */
    "/((?!api/auth|signin|_next/static|_next/image|favicon\\.ico|.*\\.(?:svg|png|ico)$).*)",
  ],
};
