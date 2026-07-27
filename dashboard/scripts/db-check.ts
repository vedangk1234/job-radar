/**
 * Prove the transaction pooler actually works before trusting a page to it.
 *
 *     npm run db:check
 *
 * The one thing this is really testing is the prepared-statement constraint.
 * Port 6543 is Supavisor in transaction mode, where a connection goes back to
 * the pool after every transaction — so a client that names its prepared
 * statements will PREPARE on one backend and EXECUTE on another, and Postgres
 * answers `prepared statement "s1" does not exist`. The failure is not
 * immediate: the first query works, and the second or third one blows up. So
 * this deliberately runs SEVERAL parameterised queries in a row, including
 * concurrent ones, rather than a single SELECT 1 that would pass either way.
 */

import { config } from "dotenv";
import postgres from "postgres";

config({ path: ".env.local" });

const dsn = (process.env.DASHBOARD_DB_URL ?? "").trim();
if (!dsn) {
  console.error("DASHBOARD_DB_URL is not set. Copy .env.local.example to .env.local.");
  process.exit(1);
}

const port = new URL(dsn.replace(/^postgres(ql)?:/, "http:")).port;
if (port !== "6543") {
  console.warn(
    `WARNING: DASHBOARD_DB_URL points at port ${port}, not 6543. ` +
      `6543 is the transaction pooler; 5432 is the session pooler the crawler uses.`,
  );
}

const sql = postgres(dsn, {
  prepare: false,
  ssl: { rejectUnauthorized: false },
  max: 3,
  connect_timeout: 15,
  onnotice: () => {},
});

async function main() {
  console.log(`connecting on port ${port} with prepare:false …\n`);

  const [{ version }] = await sql<{ version: string }[]>`SELECT version()`;
  console.log(version.split(",")[0]);

  // Several parameterised queries in a row. This is the part that fails loudly
  // if prepared statements are ever switched back on.
  for (let i = 1; i <= 5; i++) {
    const [row] = await sql<{ n: number }[]>`SELECT ${i}::int * 2 AS n`;
    if (row.n !== i * 2) throw new Error(`round trip ${i} returned ${row.n}`);
  }
  console.log("5 sequential parameterised queries  OK");

  // And concurrently, which is what actually spreads them across backends.
  const results = await Promise.all(
    [1, 2, 3, 4, 5, 6, 7, 8].map((i) => sql<{ n: number }[]>`SELECT ${i}::int AS n`),
  );
  if (results.map((r) => r[0].n).join() !== "1,2,3,4,5,6,7,8") {
    throw new Error("concurrent queries came back out of order or wrong");
  }
  console.log("8 concurrent parameterised queries   OK");

  const [counts] = await sql<
    { companies: number; tracked: number; jobs: number; prefs: number }[]
  >`
    SELECT (SELECT count(*) FROM companies)::int                  AS companies,
           (SELECT count(*) FROM companies WHERE tracked)::int    AS tracked,
           (SELECT count(*) FROM jobs)::int                       AS jobs,
           (SELECT count(*) FROM preferences)::int                AS prefs
  `;
  console.log(
    `\ncompanies=${counts.companies} (tracked ${counts.tracked})  ` +
      `jobs=${counts.jobs}  preferences rows=${counts.prefs}`,
  );

  // A text[] round trip, because the preferences editor writes three of them
  // and array binding is the other thing that can differ between drivers.
  const [prefs] = await sql<{ cities: string[]; keywords: number; max_level: number }[]>`
    SELECT cities, array_length(keywords, 1) AS keywords, max_level
      FROM preferences WHERE id = 1
  `;
  console.log(
    `preferences: cities=[${prefs.cities.join(", ")}]  ` +
      `keywords=${prefs.keywords}  max_level=${prefs.max_level}`,
  );

  console.log("\nOK — the transaction pooler works with prepared statements disabled.");
}

main()
  .catch((err) => {
    console.error("\nFAILED:", err instanceof Error ? err.message : err);
    process.exitCode = 1;
  })
  .finally(() => sql.end({ timeout: 5 }));
