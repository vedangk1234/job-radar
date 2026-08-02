import { sql } from "./db";
import { myEmail } from "./env";

/**
 * Who "the current user" is — TEMPORARY, and deleted when Phase 2 lands.
 *
 * THIS WHOLE FILE IS A PHASE-2 BRIDGE. Phase 1 gives rows an owner; Phase 2
 * gives the app a way to know who is asking. Magic-link auth will put a real
 * user id in the session, every caller of currentUserId() will read it from
 * there instead, and this file goes away. Nothing here should grow a feature.
 *
 * The TypeScript half of db/users.py — same variable, same query, same
 * lowercase comparison, so both runtimes resolve the same person from the same
 * input. Deriving the id from MY_EMAIL rather than adding a DEFAULT_USER_ID is
 * deliberate: two variables that must agree about who you are is two variables
 * that can disagree, silently, surfacing as an empty dashboard rather than an
 * error.
 *
 * MEMOISED, where the Python side is not. The asymmetry is real rather than an
 * oversight. Python has entrypoints — run_digest, enrich_matches, each script's
 * main — that resolve once and thread the id down, so "one lookup per run" is
 * structural there. A Next server has no such entrypoint: every server
 * component and every route handler is its own, and without this the id would
 * be looked up several times per page render. The promise (not the resolved
 * value) is cached so concurrent callers share one in-flight query, and a
 * REJECTION is not cached — a failed lookup during a deploy with the variable
 * still missing must not poison the instance for its whole lifetime.
 *
 * Cached on globalThis for the same reason lib/db.ts is: Next's dev server
 * re-evaluates modules on hot reload, and a module-local would be re-resolved
 * on every edit.
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

/** The public.users id for MY_EMAIL. Throws rather than guessing. */
export function currentUserId(): Promise<string> {
  if (!globalForUser.jobRadarUserId) {
    globalForUser.jobRadarUserId = resolve().catch((err) => {
      globalForUser.jobRadarUserId = undefined;
      throw err;
    });
  }
  return globalForUser.jobRadarUserId;
}
