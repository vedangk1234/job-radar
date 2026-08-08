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
  user_notified: boolean;
};

/*
 * The Python side. It builds a Preferences from each filter the dashboard sends
 * and answers is_match for every job — no database, no network, no writes. Rows
 * arrive as BYTES and are decoded utf-8 explicitly: on Windows, Python's
 * text-mode stdin defaults to the ANSI codepage, which mangles a job title with
 * an en-dash in it into a different string than the one that was sent, and the
 * resulting mismatch looks exactly like a real parity failure.
 *
 * SEVERAL FILTERS PER RUN SINCE PHASE 3 SLICE 2, in ONE subprocess. The
 * interesting variants (see VARIANTS below) all run against the same job list, so
 * paying for the interpreter and the import four times would buy nothing.
 */
const PY = `
import json, sys
sys.path.insert(0, ${JSON.stringify(REPO_ROOT)})
from notify.match import Preferences, is_match

payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
jobs = payload["jobs"]
print(json.dumps({
    "matched": [
        [j["canonical_id"] for j in jobs if is_match(j, Preferences.from_row(v["prefs"]))]
        for v in payload["variants"]
    ],
}))
`;

function python(payload: unknown): { matched: string[][] } {
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
  // The current user, resolved exactly as src/lib/currentUser.ts and
  // db/users.py both do. Inlined for the same reason findRepoRoot above is:
  // these scripts load .env.local themselves and cannot import a module that
  // opens a pool at import time. Both implementations are then compared over
  // the SAME user's filter, which is the only comparison worth making.
  const email = (process.env.MY_EMAIL ?? "").trim().toLowerCase();
  if (!email) throw new Error("MY_EMAIL is not set in .env.local");

  const [user] = await sql<{ id: string }[]>`
    SELECT id FROM public.users WHERE lower(email) = lower(${email})
  `;
  if (!user) throw new Error("MY_EMAIL matches no row in public.users — run scripts/seed_user.py");

  // The same seven columns notify/store.PREFERENCES_SQL selects. Selecting fewer
  // would hand both matchers an undefined range, which is not a state any real
  // loader can produce — the check would then be comparing two implementations on
  // an input neither will ever see.
  const [prefsRow] = await sql<Preferences[]>`
    SELECT cities, keywords, exclusions, max_level,
           exp_range_low, exp_range_high, show_no_experience_jobs
      FROM preferences WHERE user_id = ${user.id}
  `;
  if (!prefsRow) {
    throw new Error(
      "no preferences row for this user — run python scripts/backfill_preferences_user.py",
    );
  }

  // Every OPEN row, unfiltered. The SQL prefilter lib/jobs.ts uses is itself
  // part of what is being checked, so it is deliberately not applied here: if
  // it dropped a row the matcher would have kept, this is where that shows up.
  //
  // `user_notified` is carried for the one summary line below and is no part of
  // the comparison — the matcher has never read it, in either language. It used
  // to be the `j.notified` column; since Phase 1f "has this been emailed" is a
  // row in user_job_notifications for the user resolved above, so it is asked of
  // that user here too. The predicate is the same one lib/jobs.notifiedToUser
  // states for the app; this script builds its own pool for the reasons above
  // and so cannot embed that fragment. Nothing decides on it here, and
  // `npm run badge-check` is what runs the production predicate itself.
  const jobs = await sql<Row[]>`
    SELECT j.canonical_id, c.name AS company, j.title, j.city,
           j.min_experience, j.is_pm_role, j.desc_checked, j.desc_fetch_failed,
           EXISTS (
             SELECT 1
               FROM user_job_notifications n
              WHERE n.user_id = ${user.id}
                AND n.canonical_id = j.canonical_id
           ) AS user_notified
      FROM jobs j
      JOIN companies c ON c.id = j.company_id
     WHERE j.status = 'open'
     ORDER BY c.name, j.title
  `;

  console.log(
    `preferences: ${prefsRow.cities.length} cities, ${prefsRow.keywords.length} keywords, ` +
      `${prefsRow.exclusions.length} exclusions, max_level ${prefsRow.max_level}, ` +
      `experience ${prefsRow.exp_range_low}-${prefsRow.exp_range_high}, ` +
      `show_no_experience_jobs ${prefsRow.show_no_experience_jobs}`,
  );
  console.log(`open jobs   : ${jobs.length}\n`);

  /*
   * THE PREFS MATRIX — added by Phase 3 Slice 2, and the reason is that the live
   * row exercises exactly ONE corner of the rule it is supposed to be checking.
   *
   * The operator's filter is (0, 2) with the toggle on. That is the branch which
   * reproduces the pre-Slice-2 ceiling, so running only it proves the port did not
   * regress and proves nothing whatever about the three branches the new rule
   * added. A parity check that cannot reach the new code is a green light bolted
   * over an untested rule.
   *
   * So the same live rows are judged under four filters. They differ ONLY in the
   * experience fields, so any disagreement is attributable to the rule under test
   * rather than to a city or a keyword.
   */
  const VARIANTS: { label: string; note: string; prefs: Preferences }[] = [
    {
      label: "live row",
      note: "the operator's own filter — must reproduce the old `NULL or <= 2` exactly",
      prefs: prefsRow,
    },
    {
      label: "(2,5) toggle OFF",
      note: "the junior half fires, and every unread description is hidden",
      prefs: { ...prefsRow, exp_range_low: 2, exp_range_high: 5, show_no_experience_jobs: false },
    },
    {
      label: "(0,0) toggle ON",
      note: "a degenerate single-year range; boundary inclusivity at both ends",
      prefs: { ...prefsRow, exp_range_low: 0, exp_range_high: 0, show_no_experience_jobs: true },
    },
    {
      label: "unconfigured",
      note: "a range nobody has set — must match NOTHING, in both languages",
      prefs: {
        ...prefsRow,
        exp_range_low: null,
        exp_range_high: null,
        show_no_experience_jobs: true,
      },
    },
  ];

  const pythonMatched = python({ variants: VARIANTS, jobs }).matched;
  let drifted = 0;

  console.log("prefs matrix — the same open rows under four experience filters:");
  const perVariant: Set<string>[] = [];

  for (const [i, variant] of VARIANTS.entries()) {
    const fromPy = new Set(pythonMatched[i]);
    const fromTsVariant = new Set(
      jobs.filter((j) => isMatch(j, variant.prefs)).map((j) => j.canonical_id),
    );
    perVariant.push(fromTsVariant);

    const disagree = jobs.filter(
      (j) => fromPy.has(j.canonical_id) !== fromTsVariant.has(j.canonical_id),
    );
    drifted += disagree.length;

    const verdict = disagree.length ? `DRIFT x${disagree.length}` : "agree";
    console.log(
      `  ${variant.label.padEnd(18)} py ${String(fromPy.size).padStart(4)}  ` +
        `ts ${String(fromTsVariant.size).padStart(4)}  ${verdict}`,
    );
    console.log(`  ${"".padEnd(18)} ${variant.note}`);

    for (const job of disagree) {
      const py = fromPy.has(job.canonical_id);
      console.log(`    DRIFT  ${job.company} — ${job.title}`);
      console.log(
        `           python says ${py ? "MATCH" : "no"}, typescript says ${py ? "no" : "MATCH"}`,
      );
      console.log(`           ts reason: ${explain(job, variant.prefs).reason}`);
    }
  }

  // The unconfigured variant is not merely compared between the two languages —
  // both agreeing on a wrong answer would pass that. It is asserted to be EMPTY,
  // because "an unset range matches nothing" is the property, and a fallback to the
  // install's 0-2 would make both sides agree on ~38 matches instead.
  const unconfigured = perVariant[3].size;
  if (unconfigured !== 0) {
    console.error(
      `\nUNCONFIGURED RANGE MATCHED ${unconfigured} job(s). An unset experience range ` +
        `must match nothing — see notify/match.experience_excluded. Both languages ` +
        `agreeing on this number would still be the wrong answer.`,
    );
    process.exitCode = 1;
  }

  // The live row's verdict is what the rest of the report describes: the per-company
  // breakdown, the "For you" total, and the number this script exists to be
  // comparable with (preview_digest.py). The other three variants are checked above
  // and deliberately do not appear below — they are hypotheticals, not this
  // account's inbox.
  const fromTs = perVariant[0];
  console.log("");

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

  const emailed = jobs.filter((j) => fromTs.has(j.canonical_id) && j.user_notified).length;
  console.log(`\nTOTAL "For you" across all companies : ${fromTs.size}`);
  console.log(`  of those, already emailed to you   : ${emailed}  (still shown; Home is not the outbox)`);
  console.log(
    `  of those, desc_checked = false     : ${unchecked}  (shown with a "desc not checked" hint)`,
  );
  console.log(
    `  of those, description unavailable  : ${unavailable}  (asked, got nothing — kept, retried in 24h)`,
  );
  console.log(
    `\nCompare with: python scripts/preview_digest.py --dry-run --ignore-notified --no-descriptions`,
  );

  if (drifted) {
    console.error(
      `\n${drifted} disagreement(s) across the matrix. lib/match.ts and notify/match.py ` +
        `do not agree — fix the port, or change both deliberately.`,
    );
    process.exitCode = 1;
    return;
  }
  console.log(
    `\nOK — ${jobs.length} live rows x ${VARIANTS.length} filters, both implementations ` +
      `agree on every one.`,
  );
}

main()
  .catch((err) => {
    console.error("\nFAILED:", err instanceof Error ? err.message : err);
    process.exitCode = 1;
  })
  .finally(() => sql.end({ timeout: 5 }));
