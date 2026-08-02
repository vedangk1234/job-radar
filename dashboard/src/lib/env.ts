/**
 * Environment access, in one place, failing loudly.
 *
 * Every value here is required for the dashboard to be correct rather than
 * merely to boot, so a missing one throws instead of defaulting. The one that
 * matters most is ALLOWED_EMAIL: a default of "" combined with a permissive
 * comparison would turn the single-user lock into an open door, so it is read
 * through `allowedEmail()` which throws rather than returns empty.
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

/** The one account allowed to sign in. Lowercased for comparison. */
export function allowedEmail(): string {
  return required("ALLOWED_EMAIL").toLowerCase();
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
 * Not ALLOWED_EMAIL, deliberately. That variable is the sign-in gate and keeps
 * that job; identity resolves from MY_EMAIL in BOTH runtimes, because one
 * mechanism that can be wrong is better than two that can disagree. On this
 * install they hold the same address. See lib/currentUser.ts.
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
