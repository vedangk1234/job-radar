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
