import { sql } from "./db";
import { myEmail } from "./env";

/**
 * The MY_EMAIL bridge — NARROWED IN 2a, DELETED IN 2b.
 *
 * WHAT THIS FILE STOPPED BEING. Through Phase 1 this answered "who is the
 * current user" for the whole dashboard: every page render and every route
 * resolved MY_EMAIL to a public.users id through here, because there was no
 * session to ask. Phase 2a gives the request path a real answer — lib/guard.ts
 * resolves the Supabase session to a public.users row via lib/link-user.ts and
 * threads that id down explicitly — so NOTHING UNDER src/app OR src/lib CALLS
 * THIS ANY MORE. tests/test_dashboard_auth.py asserts that, because a single
 * surviving caller would be a page serving the operator's data to whoever
 * happens to be signed in.
 *
 * WHAT IT STILL IS. dashboard/scripts/*.ts are headless: db-check, match-parity
 * and badge-check run from a terminal with no request, no cookies and no
 * session, and they need to name a user to check anything about one. They ask
 * the same question db/users.py asks on the Python side, from the same
 * variable, so the two runtimes still agree about who the operator is. That is
 * the whole of the bridge that remains, and 2b removes it together with
 * db/users.py's half when the crawler stops resolving identity from the
 * environment too.
 *
 * MEMOISED, where the Python side is not — unchanged, and the asymmetry is
 * still real. Python has entrypoints that resolve once and thread the id down;
 * a Node script can call this from anywhere. The promise (not the resolved
 * value) is cached so concurrent callers share one in-flight query, and a
 * REJECTION is not cached, so a failed lookup does not poison the process.
 */

const globalForUser = globalThis as unknown as { jobRadarUserId?: Promise<string> };

async function resolve(): Promise<string> {
  const email = myEmail();

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
      "MY_EMAIL does not match any row in public.users. Run " +
        "`python scripts/seed_user.py` — it reads the same environment variable " +
        "this lookup does.",
    );
  }
  return row.id;
}

/**
 * The public.users id for MY_EMAIL. Throws rather than guessing.
 *
 * FOR dashboard/scripts/*.ts ONLY. If you are writing a page, a server
 * component or a route handler, the id you want is on the session — call
 * requirePage() or requireSession() from lib/guard.ts. Calling this from the
 * request path would hand every visitor the operator's account.
 */
export function currentUserId(): Promise<string> {
  if (!globalForUser.jobRadarUserId) {
    globalForUser.jobRadarUserId = resolve().catch((err) => {
      globalForUser.jobRadarUserId = undefined;
      throw err;
    });
  }
  return globalForUser.jobRadarUserId;
}
