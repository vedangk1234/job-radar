/**
 * Environment access, in one place, failing loudly.
 *
 * Every value here is required for the dashboard to be correct rather than
 * merely to boot, so a missing one throws instead of defaulting.
 *
 * Phase 1 needed a `soft()` wrapper alongside this, because auth.ts read these
 * at module scope and `next build` has no .env.local. Nothing does that any
 * more: the Supabase clients are constructed inside request handlers, so a
 * missing value fails a request with a legible message instead of failing a
 * build with a stack trace. That is why ALLOWED_EMAIL's fail-closed dance is
 * gone along with the variable itself.
 */

function required(name: string): string {
  const value = (process.env[name] ?? "").trim();
  if (!value) {
    throw new Error(
      `${name} is not set. Copy dashboard/.env.local.example to ` +
        `dashboard/.env.local and fill in real values.`,
    );
  }
  return value;
}

/**
 * The Supabase project URL and its publishable anon key.
 *
 * NEXT_PUBLIC_, so both are inlined into the browser bundle at BUILD time —
 * which is why they must exist on Vercel before the deploy that needs them, not
 * after. A variable added later produces a build with `undefined` baked in, and
 * that fails as a broken Supabase client rather than as a missing variable.
 *
 * PUBLISHING THE ANON KEY IS SAFE HERE FOR A REASON THAT HAD TO BE MADE TRUE.
 * An anon key is designed to be public, but only because RLS stands behind it —
 * and until Phase 2a seven of this database's ten tables had RLS off with anon
 * holding full DML on all of them. db/schema.sql's RLS block closed that, and
 * scripts/check_rls.py is what reads the answer back. The key opens /auth/v1
 * and nothing else.
 *
 * There is deliberately NO service-role key in this app. Every read and write of
 * this project's own data goes through postgres.js on the transaction pooler,
 * so the elevated key would be a credential on Vercel with nothing to do.
 */
export function supabaseUrl(): string {
  return required("NEXT_PUBLIC_SUPABASE_URL");
}

export function supabaseAnonKey(): string {
  return required("NEXT_PUBLIC_SUPABASE_ANON_KEY");
}

/**
 * The address the current user's database row is keyed by. Lowercased, and it
 * THROWS when unset — which is the whole difference between this and the other
 * reader of the same variable.
 *
 * lib/ats.ts also reads MY_EMAIL and falls back to unknown@example.com. That is
 * right for what it does: the value goes in an outbound User-Agent, so an unset
 * one makes the probe impolite and nothing more. Here the value decides WHOSE
 * preferences row is loaded, and a fallback would resolve to no user at all —
 * so it is read through required(), like every other value that has to be
 * correct rather than merely present. The fallback in ats.ts stays where it is
 * and is deliberately not reused.
 *
 * PHASE 2a NARROWED WHAT THIS IS FOR, and the narrowing is the point of the
 * sub-phase. It is no longer the dashboard request path's idea of who you are —
 * a Supabase session answers that now, per request, per person. What still
 * reads it here is the headless half: dashboard/scripts/*.ts have no request and
 * no session, so db-check, match-parity and badge-check resolve the operator's
 * own row from this variable exactly as db/users.py does on the Python side.
 * ALLOWED_EMAIL is gone entirely — any verified Google account is a real user.
 * See lib/currentUser.ts, and 2b for the removal of what remains.
 */
export function myEmail(): string {
  return required("MY_EMAIL").toLowerCase();
}

/**
 * The transaction pooler (port 6543). Not the session pooler the crawler uses:
 * a serverless dashboard opens and drops connections constantly, which is the
 * case transaction mode exists for. The cost is no prepared statements — see
 * lib/db.ts.
 */
export function dashboardDbUrl(): string {
  const dsn = required("DASHBOARD_DB_URL");
  if (dsn.includes("<ref>") || dsn.includes("<password>") || dsn.includes("<region>")) {
    throw new Error(
      "DASHBOARD_DB_URL still contains the placeholders from .env.local.example.",
    );
  }
  return dsn;
}

export function googleClientId(): string {
  return required("GOOGLE_CLIENT_ID");
}

export function googleClientSecret(): string {
  return required("GOOGLE_CLIENT_SECRET");
}
