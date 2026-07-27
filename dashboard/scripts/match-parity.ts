/**
 * Prove the dashboard's "For you" filter is the digest's filter.
 *
 *     npm run match-parity
 *
 * lib/match.ts is a port of notify/match.py, and a port is a second
 * implementation of a rule — which is a promise to drift. This runs both over
 * every open row in the LIVE database, job by job, and fails on the first
 * disagreement. Same arrangement as scripts/parity-check.ts has with
 * fetchers/resolve.py: the drift becomes a failing check rather than a "For you"
 * count that is quietly wrong about which jobs would have arrived by email.
 *
 * Real rows rather than fixtures, on purpose. The interesting inputs are the
 * titles recruiters actually write — "SDE-2 (2-5 years)", "Engineer II/III",
 * "Full Stack Builder" — and no fixture list invents those.
 *
 * The total it prints is the number Home shows, and it is directly comparable
 * to `python scripts/preview_digest.py --dry-run --ignore-notified`: both ask
 * "of everything currently open, what does the filter pick", ignoring whether a
 * job has already been emailed.
 *
 * Local-only: it shells out to the repo's Python. Not part of the Vercel build
 * and does not need to be — it is a check on a commit, not on a deploy.
 */

import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, join, resolve as resolvePath } from "node:path";

import { config } from "dotenv";
import postgres from "postgres";

import { explain, isMatch, type Preferences } from "../src/lib/match";

config({ path: ".env.local" });

/** Walk up until the Python package appears. Same helper as parity-check.ts. */
function findRepoRoot(): string {
  let dir = resolvePath(process.cwd());
  for (let i = 0; i < 6; i++) {
    if (existsSync(join(dir, "notify", "match.py"))) return dir;
    const parent = dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  throw new Error("could not find notify/match.py above the working directory");
}

const REPO_ROOT = findRepoRoot();

const dsn = (process.env.DASHBOARD_DB_URL ?? "").trim();
if (!dsn) {
  console.error("DASHBOARD_DB_URL is not set. Copy .env.local.example to .env.local.");
  process.exit(1);
}

const sql = postgres(dsn, {
  prepare: false,
  ssl: { rejectUnauthorized: false },
  max: 3,
  connect_timeout: 15,
  onnotice: () => {},
});

type Row = {
  canonical_id: string;
  company: string;
  title: string;
  city: string | null;
  min_experience: number | null;
  is_pm_role: boolean;
  desc_checked: boolean;
  desc_fetch_failed: boolean;
  notified: boolean;
};

/*
 * The Python side. It builds a Preferences from the same row the dashboard read
 * and answers is_match for each job — no database, no network, no writes. Rows
 * arrive as BYTES and are decoded utf-8 explicitly: on Windows, Python's
 * text-mode stdin defaults to the ANSI codepage, which mangles a job title with
 * an en-dash in it into a different string than the one that was sent, and the
 * resulting mismatch looks exactly like a real parity failure.
 */
const PY = `
import json, sys
sys.path.insert(0, ${JSON.stringify(REPO_ROOT)})
from notify.match import Preferences, is_match

payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
prefs = Preferences.from_row(payload["prefs"])
print(json.dumps({
    "matched": [j["canonical_id"] for j in payload["jobs"] if is_match(j, prefs)],
}))
`;

function python(payload: unknown): { matched: string[] } {
  for (const exe of ["python", "python3", "py"]) {
    const run = spawnSync(exe, ["-c", PY], {
      input: JSON.stringify(payload),
      encoding: "utf8",
      cwd: REPO_ROOT,
      maxBuffer: 32 * 1024 * 1024,
    });
    if (run.error || run.status !== 0) {
      if (run.error && "code" in run.error && run.error.code === "ENOENT") continue;
      throw new Error(`${exe} failed:\n${run.stderr || run.error}`);
    }
    return JSON.parse(run.stdout);
  }
  throw new Error("no python interpreter found on PATH");
}

async function main() {
  const [prefsRow] = await sql<Preferences[]>`
    SELECT cities, keywords, exclusions, max_level FROM preferences WHERE id = 1
  `;
  if (!prefsRow) throw new Error("no preferences row — run python scripts/init_db.py");

  // Every OPEN row, unfiltered. The SQL prefilter lib/jobs.ts uses is itself
  // part of what is being checked, so it is deliberately not applied here: if
  // it dropped a row the matcher would have kept, this is where that shows up.
  const jobs = await sql<Row[]>`
    SELECT j.canonical_id, c.name AS company, j.title, j.city,
           j.min_experience, j.is_pm_role, j.desc_checked, j.desc_fetch_failed,
           j.notified
      FROM jobs j
      JOIN companies c ON c.id = j.company_id
     WHERE j.status = 'open'
     ORDER BY c.name, j.title
  `;

  console.log(
    `preferences: ${prefsRow.cities.length} cities, ${prefsRow.keywords.length} keywords, ` +
      `${prefsRow.exclusions.length} exclusions, max_level ${prefsRow.max_level}`,
  );
  console.log(`open jobs   : ${jobs.length}\n`);

  const fromPython = new Set(python({ prefs: prefsRow, jobs }).matched);
  const fromTs = new Set(jobs.filter((j) => isMatch(j, prefsRow)).map((j) => j.canonical_id));

  const disagreements = jobs.filter(
    (j) => fromPython.has(j.canonical_id) !== fromTs.has(j.canonical_id),
  );

  for (const job of disagreements) {
    const py = fromPython.has(job.canonical_id);
    console.log(`  DRIFT  ${job.company} — ${job.title}`);
    console.log(`         python says ${py ? "MATCH" : "no"}, typescript says ${py ? "no" : "MATCH"}`);
    console.log(`         ts reason: ${explain(job, prefsRow).reason}`);
  }

  const byCompany = new Map<string, number>();
  let unchecked = 0;
  let unavailable = 0;
  for (const job of jobs) {
    if (!fromTs.has(job.canonical_id)) continue;
    byCompany.set(job.company, (byCompany.get(job.company) ?? 0) + 1);
    if (!job.desc_checked) unchecked += 1;
    else if (job.desc_fetch_failed) unavailable += 1;
  }

  console.log("matches per company (what Home shows):");
  for (const [company, n] of [...byCompany].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))) {
    console.log(`  ${String(n).padStart(3)}  ${company}`);
  }

  const notified = jobs.filter((j) => fromTs.has(j.canonical_id) && j.notified).length;
  console.log(`\nTOTAL "For you" across all companies : ${fromTs.size}`);
  console.log(`  of those, already emailed          : ${notified}  (still shown; Home is not the outbox)`);
  console.log(
    `  of those, desc_checked = false     : ${unchecked}  (shown with a "desc not checked" hint)`,
  );
  console.log(
    `  of those, description unavailable  : ${unavailable}  (asked, got nothing — kept, retried in 24h)`,
  );
  console.log(
    `\nCompare with: python scripts/preview_digest.py --dry-run --ignore-notified --no-descriptions`,
  );

  if (disagreements.length) {
    console.error(
      `\n${disagreements.length} disagreement(s). lib/match.ts and notify/match.py do not ` +
        `agree — fix the port, or change both deliberately.`,
    );
    process.exitCode = 1;
    return;
  }
  console.log(`\nOK — ${jobs.length} live rows, both implementations agree on every one.`);
}

main()
  .catch((err) => {
    console.error("\nFAILED:", err instanceof Error ? err.message : err);
    process.exitCode = 1;
  })
  .finally(() => sql.end({ timeout: 5 }));
