import { createServerClient } from "@supabase/ssr";
import { NextResponse, type NextRequest } from "next/server";

import { supabaseAnonKey, supabaseUrl } from "@/lib/env";

/**
 * The Supabase client for middleware, plus the session refresh.
 *
 * Middleware is the only place in a Next app that can both read the request's
 * cookies and write cookies onto the response, which makes it the only place a
 * rotating auth token can actually be renewed. Server components can read but
 * not write (see lib/supabase/server.ts), so without this the access token
 * would expire mid-visit and the next page would bounce to /signin for no
 * reason a user could understand.
 *
 * THE RESPONSE OBJECT RETURNED HERE IS THE ONE THAT MUST BE SENT. setAll
 * rebuilds it, because a refreshed token has to travel on the response that
 * actually reaches the browser. Constructing a fresh NextResponse.next() in the
 * caller and returning that instead silently drops every renewed cookie — the
 * app keeps working right up until the first token expiry, and then logs
 * everyone out on a timer. That is why this returns the response rather than
 * just the user.
 */
export async function updateSession(request: NextRequest) {
  let response = NextResponse.next({ request });

  const supabase = createServerClient(supabaseUrl(), supabaseAnonKey(), {
    cookies: {
      getAll() {
        return request.cookies.getAll();
      },
      setAll(cookiesToSet) {
        for (const { name, value } of cookiesToSet) {
          request.cookies.set(name, value);
        }
        response = NextResponse.next({ request });
        for (const { name, value, options } of cookiesToSet) {
          response.cookies.set(name, value, options);
        }
      },
    },
  });

  // getUser() and not getSession(): this call is what verifies the JWT, and it
  // is also what triggers the refresh. Removing it "because the user is not
  // used here" would turn this function into a no-op that still looks correct.
  const {
    data: { user },
  } = await supabase.auth.getUser();

  return { response, user };
}
