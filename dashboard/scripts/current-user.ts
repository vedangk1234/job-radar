import { sql } from "../src/lib/db";

/**
 * The operator's own public.users id, from MY_EMAIL. FOR THE CHECK SCRIPTS ONLY.
 *
 * WHY THIS FILE IS IN scripts/ AND NOT IN src/lib/. It was src/lib/currentUser.ts
 * through Phase 1, when it answered "who is the current user" for the whole
 * dashboard: every page render and every route resolved MY_EMAIL to a
 * public.users id through it, because there was no session to ask. 2a gave the
 * request path a real answer — lib/guard.ts resolves the Supabase session to a
 * public.users row via lib/link-user.ts and threads that id down — and 2b
 * finishes the job by moving the lookup out of the directory the request path
 * lives in. A page that called this would serve the operator's filter, watch list
 * and job history to whichever stranger happened to be signed in, and it would
 * look completely normal to the operator; now there is nothing under src/ to
 * call, which is a stronger statement than a rule saying not to.
 *
 * WHAT IT IS STILL FOR. dashboard/scripts/*.ts are headless: db-check,
 * match-parity, badge-check and ui-check run from a terminal with no request, no
 * cookies and no session, and they have to name a user in order to check
 * anything about one. They ask what db/users.py asks on the Python side, from the
 * same variable, so the two runtimes still agree about who the operator is. That
 * narrow, session-less use is the whole of what survives.
 *
 * NOT MEMOISED any more, and it does not need to be. The promise-caching in the
 * old file bought one guarantee — "one lookup per process" — for a Next server
 * that has no single entrypoint, where every server component and route handler
 * is its own. A script has a main(); it resolves once at the top and passes the
 * id down, exactly as db/users.py's callers do, so the cache would be hidden
 * state buying nothing.
 *
 * MY_EMAIL is read here rather than through lib/env, which no longer exports it:
 * scripts/db-check.ts and scripts/match-parity.ts already read the variable
 * directly for the same reason, and leaving an identity read in src/lib/env.ts
 * with no caller in src/ would be an invitation.
 */
export async function currentUserId(): Promise<string> {
  const email = (process.env.MY_EMAIL ?? "").trim().toLowerCase();
  if (!email) {
    throw new Error(
      "MY_EMAIL is not set in dashboard/.env.local, so the operator's account " +
        "cannot be named. This is a check script; the app itself does not read it.",
    );
  }

  // Schema-qualified public.users, never a bare `users`. Supabase ships its own
  // auth.users, so what a bare reference resolves to is a property of the
  // connection's search_path rather than of this file. lower() on both sides
  // matches users_email_lower_idx in db/schema.sql, so a MY_EMAIL that differs
  // from the seeded row only in case still resolves.
  const [row] = await sql<{ id: string }[]>`
    SELECT id FROM public.users WHERE lower(email) = lower(${email})
  `;

  if (!row) {
    throw new Error(
      "MY_EMAIL does not match any row in public.users. Sign in once with that " +
        "address, or run `python scripts/seed_user.py` — it reads the same " +
        "environment variable this lookup does.",
    );
  }
  return row.id;
}
