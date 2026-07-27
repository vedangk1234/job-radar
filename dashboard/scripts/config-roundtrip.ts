/**
 * Prove that a company added through the dashboard stores `config` as a jsonb
 * OBJECT — the shape every reader assumes and the one the crawler needs.
 *
 *     npm run config-roundtrip
 *
 * WHY THIS EXISTS
 * ---------------
 * upsertCompany used to bind config as `${JSON.stringify(config)}::jsonb`.
 * postgres.js resolves that parameter to jsonb and serialises jsonb with its own
 * JSON.stringify, so the value was encoded twice and the column ended up holding
 * a jsonb *string* containing JSON text. `jsonb_typeof` said 'string'.
 *
 * Nothing on this side of the stack noticed. The dashboard read the row back
 * through postgres.js, which JSON.parsed it once and handed the page an object,
 * so every screen looked right. The failure surfaced 30 minutes later and in a
 * different language: asyncpg returns jsonb as text, Company.from_row decoded it
 * once into a `str`, and `config.get("host")` raised
 * `'str' object has no attribute 'get'`. Guard 1 logged it as a fetch error and
 * left the jobs table alone, so the company crawled to nothing indefinitely
 * while looking like an unreachable board rather than a bad row. LSEG sat at
 * zero jobs for exactly this reason.
 *
 * A round trip through postgres.js alone would therefore have PASSED against the
 * bug. So the assertion here is deliberately not "what I put in comes back out"
 * — it is `jsonb_typeof(config) = 'object'`, asked of Postgres, which is the
 * only question whose answer the Python side and the TypeScript side share.
 *
 * The empty config is checked too, and it is not redundant: `'"{}"'::jsonb` is
 * not `'{}'::jsonb`, so a double-encoded empty config also defeats the
 * ON CONFLICT rule in upsertCompany that refuses to overwrite a stored config
 * with a blank one. A re-add by NAME could then blank a Workday row's host/site.
 *
 * Writes real rows and deletes them in a finally block. The tokens are sentinels
 * that no board could ever answer to, and both are removed up-front as well, so
 * a run that was killed mid-way cannot make the next one fail.
 */

import { config as loadEnv } from "dotenv";

loadEnv({ path: ".env.local" });

/**
 * src/lib/db.ts builds its connection pool at MODULE SCOPE, and static imports
 * are hoisted above the loadEnv() call above — so importing it at the top would
 * evaluate dashboardDbUrl() against an empty process.env and throw before this
 * file's first statement runs. Hence the deferred import inside main().
 *
 * This is the same class of quirk parity-check.ts documents about `import.meta`:
 * tsx transpiles to CJS on this Node version, and the difference shows.
 */
type Lib = {
  sql: typeof import("../src/lib/db").sql;
  upsertCompany: typeof import("../src/lib/companies").upsertCompany;
};

async function loadLib(): Promise<Lib> {
  const [db, companies] = await Promise.all([
    import("../src/lib/db"),
    import("../src/lib/companies"),
  ]);
  return { sql: db.sql, upsertCompany: companies.upsertCompany };
}

/** Not a real tenant. Nothing will ever crawl these. */
const PROBE_TOKENS = ["__roundtrip_workday__", "__roundtrip_named__"];

const WORKDAY_CONFIG = {
  host: "roundtrip.wd1.myworkdayjobs.com",
  site: "External_Careers",
  lang: "en-US",
  careers_url: "https://roundtrip.wd1.myworkdayjobs.com/en-US/External_Careers",
};

type Check = { label: string; ok: boolean; detail: string };
const checks: Check[] = [];

function record(label: string, ok: boolean, detail: string) {
  checks.push({ label, ok, detail });
  console.log(`  ${ok ? "ok  " : "FAIL"} ${label.padEnd(46)} ${detail}`);
}

let lib: Lib;

async function cleanup() {
  if (!lib) return;
  await lib.sql`DELETE FROM companies WHERE token = ANY(${PROBE_TOKENS})`;
}

async function storedShape(ats: string, token: string) {
  const [row] = await lib.sql<{ jtype: string; raw: string }[]>`
    SELECT jsonb_typeof(config) AS jtype, config::text AS raw
      FROM companies WHERE ats = ${ats} AND token = ${token}
  `;
  return row;
}

async function main() {
  lib = await loadLib();
  const { sql, upsertCompany } = lib;

  await cleanup(); // in case a previous run was interrupted

  console.log("add-by-URL (workday), through upsertCompany:");
  const workday = await upsertCompany({
    name: "Roundtrip Probe",
    ats: "workday",
    token: PROBE_TOKENS[0],
    config: WORKDAY_CONFIG,
    category: "gcc",
    indiaScoped: true,
  });
  record("insert reported as new", workday.inserted, `id=${workday.id}`);

  const wdShape = await storedShape("workday", PROBE_TOKENS[0]);
  record("jsonb_typeof(config) is 'object'", wdShape.jtype === "object", wdShape.jtype);
  record(
    "config is not a JSON string in disguise",
    !wdShape.raw.startsWith('"'),
    wdShape.raw.slice(0, 78),
  );

  // Every key the Workday fetcher reads survived the trip with its value intact.
  const [keys] = await sql<{ host: string | null; site: string | null; lang: string | null }[]>`
    SELECT config->>'host' AS host, config->>'site' AS site, config->>'lang' AS lang
      FROM companies WHERE ats = 'workday' AND token = ${PROBE_TOKENS[0]}
  `;
  record(
    "->> reaches host/site/lang (not nested text)",
    keys.host === WORKDAY_CONFIG.host &&
      keys.site === WORKDAY_CONFIG.site &&
      keys.lang === WORKDAY_CONFIG.lang,
    `host=${keys.host} site=${keys.site} lang=${keys.lang}`,
  );

  console.log("\nadd-by-NAME (empty config), through upsertCompany:");
  const named = await upsertCompany({
    name: "Roundtrip Named Probe",
    ats: "greenhouse",
    token: PROBE_TOKENS[1],
    config: {},
    category: "unknown",
    indiaScoped: false,
  });
  const emptyShape = await storedShape("greenhouse", PROBE_TOKENS[1]);
  record("insert reported as new", named.inserted, `id=${named.id}`);
  record("jsonb_typeof(config) is 'object'", emptyShape.jtype === "object", emptyShape.jtype);
  record("stored as {} exactly", emptyShape.raw === "{}", emptyShape.raw);

  // The ON CONFLICT rule keys off `EXCLUDED.config = '{}'::jsonb`, which a
  // double-encoded empty config would never satisfy. Re-adding the WORKDAY row
  // by name — no config — must therefore leave its host/site alone.
  console.log("\nre-add with an empty config must not blank a stored one:");
  const again = await upsertCompany({
    name: "Roundtrip Probe",
    ats: "workday",
    token: PROBE_TOKENS[0],
    config: {},
    category: "gcc",
    indiaScoped: true,
  });
  const afterShape = await storedShape("workday", PROBE_TOKENS[0]);
  record("recognised as an update, not an insert", !again.inserted, `id=${again.id}`);
  record(
    "host/site preserved through the empty re-add",
    afterShape.raw === wdShape.raw,
    afterShape.raw.slice(0, 78),
  );

  // Byte-shape parity with a row the Python scripts wrote. Both go through
  // Postgres's own jsonb normaliser, so a correct row from either writer is
  // indistinguishable from the other; a double-encoded one is not.
  const [adobe] = await sql<{ jtype: string }[]>`
    SELECT jsonb_typeof(config) AS jtype FROM companies WHERE ats = 'workday' AND token = 'adobe'
  `;
  if (adobe) {
    record(
      "same jsonb_typeof as a script-added row (Adobe)",
      adobe.jtype === wdShape.jtype,
      `adobe=${adobe.jtype} probe=${wdShape.jtype}`,
    );
  }

  // And nothing already in the table is mis-shaped.
  console.log("\nthe live table:");
  const bad = await sql<{ id: number; name: string; jtype: string }[]>`
    SELECT id, name, jsonb_typeof(config) AS jtype
      FROM companies
     WHERE jsonb_typeof(config) <> 'object' AND token <> ALL(${PROBE_TOKENS})
     ORDER BY id
  `;
  record(
    "every companies.config is an object",
    bad.length === 0,
    bad.length === 0 ? "all rows" : bad.map((r) => `${r.name}=${r.jtype}`).join(", "),
  );
}

main()
  .then(async () => {
    await cleanup();
    const failed = checks.filter((c) => !c.ok);
    if (failed.length) {
      console.error(`\n${failed.length} check(s) FAILED:`);
      for (const c of failed) console.error(`  - ${c.label}: ${c.detail}`);
      process.exitCode = 1;
      return;
    }
    console.log(`\nOK — ${checks.length} checks, config round-trips as a jsonb object.`);
  })
  .catch(async (err) => {
    console.error("\nFAILED:", err instanceof Error ? err.message : err);
    process.exitCode = 1;
    await cleanup().catch(() => {});
  })
  .finally(() => lib?.sql.end({ timeout: 5 }));
