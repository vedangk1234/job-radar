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

/*
 * THERE IS NO myEmail() HERE ANY MORE, and its absence is the point of 2b.
 *
 * It existed to answer "whose preferences row is this", which through Phase 1
 * meant resolving MY_EMAIL to a public.users id — the identity bridge. 2a made
 * the session the answer for every request; 2b removed the last caller from
 * src/ and moved the lookup itself to dashboard/scripts/current-user.ts, where
 * the only things without a session live. An exported identity read with no
 * caller in this directory would be an invitation to the next page that needs a
 * user id in a hurry, and that page would serve the operator's data to whoever
 * was signed in.
 *
 * MY_EMAIL is still read in two places, neither of them identity: lib/ats.ts
 * puts it in an outbound User-Agent (falling back to unknown@example.com,
 * because an unset one makes a probe impolite and nothing more), and the check
 * scripts read it from process.env directly to name the operator's own account.
 */

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
