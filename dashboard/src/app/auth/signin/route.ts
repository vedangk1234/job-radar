import { NextResponse } from "next/server";

import { createClient } from "@/lib/supabase/server";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * Where sign-in STARTS. A Route Handler, and it has to be one.
 *
 * WHY THIS IS NOT A SERVER ACTION ANY MORE. It was, and the flow never
 * completed once. `signInWithOAuth` does two things: it mints a PKCE code
 * verifier and persists it as a cookie, and it returns the provider URL to send
 * the browser to. Both halves have to survive the response — and from a Server
 * Action the second one did not. `redirect()` in a Server Action is not an HTTP
 * redirect the browser follows; it is a 303 the CLIENT ROUTER interprets, and
 * for an external URL that navigation bounced straight back to /signin. The
 * request log said so plainly once we read it as a pair:
 *
 *     POST /signin 303        <- the action ran
 *     GET  /signin 200        <- and the router landed back where it started
 *
 * with no row in auth.flow_state, which is the proof it never reached Supabase's
 * /auth/v1/authorize at all. Google was never involved; the verifier was never
 * going to be redeemed, because no code was ever issued for it.
 *
 * A Route Handler returns a real 307 with a real Location, so an external
 * destination is the browser's problem rather than the router's — and cookies
 * set while handling it ride out on that same response. Verified before this
 * file existed, by a probe route that returned the Set-Cookie headers as JSON:
 * the verifier lands with `Path=/; SameSite=lax` and NO `Secure` flag, which is
 * exactly what has to be true for it to survive the cross-site return from
 * accounts.google.com on http://localhost.
 *
 * WHAT SURVIVED THE MOVE. Reached by a plain GET form on /signin, so it still
 * needs no client-side Supabase instance, no session listener and no JavaScript
 * — the argument the Server Action was chosen for in the first place is
 * unchanged, and this satisfies it better. `next` is still sanitised path-only,
 * the same rule stated in the same words in middleware.ts and auth/callback,
 * because each is separately reachable.
 */
export async function GET(request: Request) {
  const url = new URL(request.url);
  const rawNext = url.searchParams.get("next") ?? "/";
  // Path-only, so a crafted ?next= cannot bounce the sign-in through to another
  // origin. "//evil.com" is protocol-relative, hence the second check.
  const next = rawNext.startsWith("/") && !rawNext.startsWith("//") ? rawNext : "/";

  // The origin is derived from the request rather than from a configured base
  // URL, so localhost and the Vercel domain both work with no extra variable to
  // keep in sync. The forwarded headers are read first because behind Vercel's
  // proxy the request URL's own protocol is not the one the browser used, and a
  // redirectTo of http://… would not match the Redirect URLs allowlist. That
  // allowlist, not this line, is what actually constrains where a session can be
  // delivered.
  const forwardedHost = request.headers.get("x-forwarded-host");
  const forwardedProto = request.headers.get("x-forwarded-proto");
  const origin = forwardedHost
    ? `${forwardedProto ?? "https"}://${forwardedHost}`
    : url.origin;

  const supabase = await createClient();
  const { data, error } = await supabase.auth.signInWithOAuth({
    provider: "google",
    options: {
      redirectTo: `${origin}/auth/callback?next=${encodeURIComponent(next)}`,
      // Google's own account chooser, every time. Without it a browser already
      // signed into one account signs straight back into it, which makes
      // "switch account" impossible to reach and looks like the page ignoring
      // the click. Carried over from the Phase-1 Auth.js config.
      queryParams: { prompt: "select_account" },
    },
  });

  if (error || !data?.url) {
    console.error("[auth/signin] signInWithOAuth failed", error);
    return NextResponse.redirect(
      new URL(`/signin?error=${encodeURIComponent(error?.message ?? "no_redirect_url")}`, origin),
    );
  }

  console.log(`[auth/signin] redirecting to ${new URL(data.url).host} with next=${next}`);
  return NextResponse.redirect(data.url);
}
