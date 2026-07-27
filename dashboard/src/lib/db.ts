import postgres from "postgres";

import { dashboardDbUrl } from "./env";

/**
 * The dashboard's connection to the same Supabase database the crawler writes.
 *
 * TRANSACTION POOLER, AND WHY `prepare: false` IS NOT OPTIONAL
 * ------------------------------------------------------------
 * DASHBOARD_DB_URL points at port 6543 — Supavisor in transaction mode. In that
 * mode a server connection is handed back to the pool at the end of every
 * transaction, so two statements from this client can land on two different
 * backends. Prepared statements are per-backend state: PREPARE lands on backend
 * A, EXECUTE goes to backend B, and Postgres answers
 * `prepared statement "s1" does not exist`. It is not a slow leak — it fails on
 * roughly the second query, under exactly the concurrency a dashboard produces.
 *
 * `prepare: false` makes postgres.js send everything through the unnamed
 * portal, which carries no cross-statement state. Parameters are still sent
 * out-of-band, so sql`...` interpolation is still parameterised and still
 * injection-safe — the only thing given up is the plan cache.
 *
 * This mirrors `statement_cache_size=0` in db/connection.py on the Python side.
 * Same database, same constraint, two clients.
 *
 * SSL: the pooler presents a certificate signed by Supabase's own private CA,
 * which is in no system trust store, so verification is off and the connection
 * is merely encrypted — libpq's `sslmode=require`. Identical to the reasoning
 * in db/connection.py:_ssl_context.
 */
function connect() {
  return postgres(dashboardDbUrl(), {
    prepare: false,
    ssl: { rejectUnauthorized: false },
    // A dashboard is read-heavy and low-traffic. A small pool keeps well clear
    // of Supabase's free-tier client limit even with several tabs open.
    max: 3,
    idle_timeout: 20,
    connect_timeout: 15,
    onnotice: () => {},
  });
}

// Next's dev server re-evaluates modules on every hot reload. Without this the
// old pool is orphaned rather than closed and the connection count climbs until
// Supabase starts refusing. Cached on globalThis, which survives the reload.
const globalForDb = globalThis as unknown as { jobRadarSql?: ReturnType<typeof connect> };

export const sql = globalForDb.jobRadarSql ?? connect();

if (process.env.NODE_ENV !== "production") {
  globalForDb.jobRadarSql = sql;
}
