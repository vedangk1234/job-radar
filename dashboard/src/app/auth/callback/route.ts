import { NextResponse } from "next/server";

import { createClient } from "@/lib/supabase/server";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * Where Google sends you back to, via Supabase. The one route that must work
 * while signed out, and the only place a session is created.
 *
 * The handshake in full: /signin posts a server action, which calls
 * signInWithOAuth and redirects the browser to Google; Google authenticates and
 * returns to Supabase's own /auth/v1/callback; Supabase redirects here with a
 * one-time `code`; this exchanges that code for a session and sets the cookies.
 * The redirect URI registered in Google Cloud is SUPABASE's, not this route —
 * this route is only ever reached from Supabase, which is why it appears in
 * Supabase's Redirect URLs allowlist and not in Google's.
 *
 * `next` is sanitised the same way lib/guard and middleware sanitise it, and
 * for the same reason: it arrives on a query string, and a URL that survives a
 * sign-in is the most attractive open redirect in any app. Path-only, and the
 * protocol-relative "//evil.com" form is rejected explicitly because it starts
 * with "/" and would otherwise pass.
 *
 * No linking happens here. A person can arrive at any page with a valid cookie
 * and never come through this route again, so making the callback responsible
 * for creating their public.users row would put the one write that matters on
 * the one path that runs least. lib/guard.ts does it on every authenticated
 * request instead — see lib/link-user.ts.
 */
export async function GET(request: Request) {
  const url = new URL(request.url);
  const code = url.searchParams.get("code");
  const rawNext = url.searchParams.get("next") ?? "/";
  const next = rawNext.startsWith("/") && !rawNext.startsWith("//") ? rawNext : "/";

  // EVERY EXIT SAYS WHICH EXIT IT WAS, and that is the whole of the logging kept
  // here. This route used to log only on a failed exchange, so a bail-out and a
  // success were the same silent 307 in a request log — which is exactly how a
  // sign-in that never happened once read as a sign-in that did. Whether the
  // code arrived, and whether it was redeemed, is enough to tell those apart.
  //
  // Deliberately NOT dumping cookie names: it was invaluable while the PKCE
  // verifier was going missing and it is request-scoped session material, which
  // is not something to write to a log on every sign-in once the question is
  // answered.
  console.log(`[auth/callback] code=${code ? "present" : "ABSENT"} next=${next}`);

  // Supabase reports a refused or abandoned consent as query parameters rather
  // than as a failed exchange, so they are read before anything else — without
  // this, cancelling at Google's screen lands on "no code supplied", which is
  // true and unhelpful.
  const error = url.searchParams.get("error_description") ?? url.searchParams.get("error");
  if (error) {
    console.error("[auth/callback] the provider returned an error, no exchange attempted:", error);
    return NextResponse.redirect(
      new URL(`/signin?error=${encodeURIComponent(error)}`, url.origin),
    );
  }

  if (!code) {
    console.error("[auth/callback] no code on the query string, no exchange attempted");
    return NextResponse.redirect(new URL("/signin?error=no_code", url.origin));
  }

  const supabase = await createClient();
  const { error: exchangeError } = await supabase.auth.exchangeCodeForSession(code);

  if (exchangeError) {
    console.error(
      `[auth/callback] code exchange FAILED: ${exchangeError.message} ` +
        `(status ${exchangeError.status ?? "none"}, code ${exchangeError.code ?? "none"})`,
      exchangeError,
    );
    return NextResponse.redirect(
      new URL(`/signin?error=${encodeURIComponent(exchangeError.message)}`, url.origin),
    );
  }

  console.log(`[auth/callback] code exchange OK, session created; redirecting to ${next}`);
  return NextResponse.redirect(new URL(next, url.origin));
}
