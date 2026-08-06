import { createServerClient } from "@supabase/ssr";
import { cookies } from "next/headers";

import { supabaseAnonKey, supabaseUrl } from "@/lib/env";

/**
 * The Supabase client for server components and route handlers.
 *
 * Created PER REQUEST rather than once at module scope, which is not a style
 * choice: the client carries the caller's cookies, so a module-level singleton
 * on a server handling two people at once would hand one of them the other's
 * session. lib/db.ts is a singleton for exactly the opposite reason — a
 * connection pool belongs to the process and to nobody in particular.
 *
 * THE ONLY THING THIS CLIENT IS FOR IS AUTH. Every read and write of this
 * project's own data goes through postgres.js on the transaction pooler, as it
 * did through the whole of Phase 1. That is why no service-role key exists
 * anywhere in this app and why the anon key can be published: it opens
 * /auth/v1 and, since the RLS block in db/schema.sql, nothing else. See
 * scripts/check_rls.py.
 *
 * `cookieWrites` says whether this caller can actually write cookies, and the
 * default is the safe half of that answer. A page or a layout renders in a
 * context where Next refuses the write, so "best-effort" means a failed write is
 * logged and swallowed. Only a Route Handler minting or redeeming a PKCE flow
 * passes "load-bearing", which restores the re-throw for the code verifier. See
 * the long note in setAll for the outage that made the distinction necessary.
 */
export async function createClient({
  cookieWrites = "best-effort",
}: { cookieWrites?: "best-effort" | "load-bearing" } = {}) {
  const cookieStore = await cookies();

  return createServerClient(supabaseUrl(), supabaseAnonKey(), {
    cookies: {
      getAll() {
        return cookieStore.getAll();
      },
      setAll(cookiesToSet) {
        // ONE try PER COOKIE, NOT ONE AROUND THE LOOP. The original wrapped the
        // whole loop, so the first name that threw abandoned every name after
        // it — and said nothing. Two cookies commonly arrive together here (the
        // auth token and its chunk, or a verifier alongside a refresh), and a
        // partial write is the state that produces a flow which is neither
        // signed in nor cleanly failed.
        for (const { name, value, options } of cookiesToSet) {
          try {
            cookieStore.set(name, value, options);
          } catch (err) {
            // A Server Component cannot set cookies, and Next throws if it
            // tries. For a rotating auth token that is genuinely harmless —
            // middleware.ts refreshes on every request, so the write has
            // already happened somewhere it was allowed to.
            //
            // IT IS NOT HARMLESS FOR THE PKCE CODE VERIFIER, and conflating the
            // two is what cost a whole debugging session. The verifier is
            // written once, by the sign-in route, and is the only thing that
            // can redeem the code Google sends back. If that write is dropped
            // the flow does not fail here — it fails minutes later at
            // /auth/callback with a code and nothing to exchange it with, and
            // the swallow meant there was no line anywhere saying so.
            //
            // So: everything is logged, and a lost verifier is re-thrown —
            // WHERE A VERIFIER IS ACTUALLY BEING WRITTEN. That qualifier is the
            // whole of `cookieWrites`, and it cost a production outage to learn.
            //
            // Re-throwing unconditionally took /signin down with a
            // FUNCTION_INVOCATION_TIMEOUT. getUser() on a page with an expiring
            // token refreshes, and the refresh path calls _removeSession, which
            // CLEARS the auth cookies — the code-verifier among them. So a
            // render that was only ever reading a session hit the fatal branch
            // on a cookie it was deleting, not minting, and the throw escaped
            // mid-getUser() inside the page's own render. Nothing above it
            // catches that: the render never settles and Vercel kills the
            // invocation. A verifier the refresh is discarding cannot be lost,
            // because nothing is waiting to redeem it.
            //
            // Renders and Server Actions therefore log and carry on; the two
            // auth Route Handlers, where a dropped verifier really does strand
            // a sign-in, ask for "load-bearing" and keep the loud failure.
            const fatal = name.includes("code-verifier");
            const loud = fatal && cookieWrites === "load-bearing";
            console.error(
              `[supabase/cookies] could not set "${name}"` +
                (loud ? " — THIS IS THE PKCE CODE VERIFIER; the sign-in cannot complete without it" : ""),
              err instanceof Error ? err.message : err,
            );
            if (loud) throw err;
          }
        }
      },
    },
  });
}

/**
 * The authenticated user, or null — and it is getUser(), never getSession().
 *
 * getSession() reads the auth cookie and decodes it WITHOUT verifying the
 * signature, because it is built for the browser where the cookie came from
 * your own storage. On a server the cookie came from the network, so trusting
 * it means anybody who can write a cookie is anybody they say they are.
 * getUser() verifies the JWT against the auth server. Every gate in this app
 * calls this function and no gate calls getSession() — tests/test_dashboard_auth
 * asserts the string does not appear in src/ at all, because this is the kind of
 * mistake that is one autocomplete away and produces no symptom.
 */
export async function getAuthUser() {
  const supabase = await createClient();
  const {
    data: { user },
  } = await supabase.auth.getUser();
  return user;
}
