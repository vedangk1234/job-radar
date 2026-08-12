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
  work_mode: string | null;
  min_experience: number | null;
  is_pm_role: boolean;
  // postgres.js returns NUMERIC as a string; see lib/match.MatchableJob.
  salary_min: string | null;
  salary_max: string | null;
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
 * paying for the interpreter and the import once per variant would buy nothing.
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

/** notify/match.NON_INDIA_LABEL, spelled locally — this script imports no app module with a pool. */
const NON_INDIA_LABEL_LOCAL = "non-india";

/**
 * A fabricated row with the shape both matchers read. Never inserted anywhere.
 *
 * The title and the description facts are chosen to pass every OTHER test, so a
 * failure can only be attributable to the city gate and the work-mode rule —
 * the same discipline the VARIANTS follow by differing in one field at a time.
 */
function synthetic(
  id: string,
  city: string,
  workMode: string | null,
  salary: [string | null, string | null] = [null, null],
  title: string = PROBE_TITLE,
): Row {
  return {
    canonical_id: id,
    company: "(synthetic)",
    title,
    city,
    work_mode: workMode,
    salary_min: salary[0],
    salary_max: salary[1],
    min_experience: null,
    is_pm_role: false,
    desc_checked: true,
    desc_fetch_failed: false,
    user_notified: false,
  };
}

/**
 * A title that passes the TITLE STAGE of whatever filter a probe is judged under.
 *
 * IT USED TO BE THE LITERAL "Backend Engineer" AND THAT WAS A LATENT BUG, found
 * by the filter-first slice's live run rather than caused by it. Every probe is
 * judged against a variant of the OPERATOR'S OWN preferences row, so the title has
 * to match a keyword that row actually holds — and that row is editable from the
 * dashboard. On 2026-08-11 it was edited from 43 keywords to 25, `backend` was not
 * among the survivors, and all six expected-MATCH probes began failing in BOTH
 * languages at once. Both agreeing is what says it is a fixture bug rather than
 * parity drift, and it is the shape of failure this file exists to make loud: a
 * probe that cannot fire is a green light bolted over an untested rule.
 *
 * So the title is DERIVED from the filter under test. The first keyword that no
 * exclusion swallows is the whole rule — the probes exist to exercise the CITY,
 * WORK-MODE and SALARY branches, and the title stage is scaffolding that must
 * never be the thing that fails.
 *
 * Set once from the live row, before the probes are built. If it is ever empty the
 * run refuses rather than quietly testing nothing.
 */
let PROBE_TITLE = "";

function probeTitle(prefs: Preferences): string {
  const usable = prefs.keywords.find(
    (kw) => !prefs.exclusions.some((ex) => kw.includes(ex)),
  );
  if (!usable) {
    throw new Error(
      "no keyword on the live preferences row survives its own exclusions, so no " +
        "synthetic probe can pass the title stage. The probes would all fail for a " +
        "reason that has nothing to do with the rules they test.",
    );
  }
  return usable;
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

  // The same ELEVEN columns notify/store.PREFERENCES_SQL selects — eleven since
  // the salary slice added salary_bands to the ten the work-mode slice left.
  // Selecting fewer would hand both matchers an undefined range or an undefined
  // city answer, which is not a state any real loader can produce — the check would
  // then be comparing two implementations on an input neither will ever see.
  const [prefsRow] = await sql<Preferences[]>`
    SELECT cities, selected_cities, all_india,
           keywords, exclusions, max_level,
           exp_range_low, exp_range_high, show_no_experience_jobs,
           work_modes, salary_bands
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
    SELECT j.canonical_id, c.name AS company, j.title, j.city, j.work_mode,
           j.salary_min, j.salary_max,
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
  // Derived from the live row before any probe is built — see probeTitle. Every
  // VARIANT below is a spread of prefsRow that changes ONE field at a time, and
  // none of them touches `keywords`, so one title is correct for all of them.
  PROBE_TITLE = probeTitle(prefsRow);

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

    /*
     * THE CITY VARIANTS — cities Slice B, and the same argument the experience
     * matrix makes one field over. The live row is a three-city selection, which is
     * ONE of the gate's three branches; running only it would prove the port did
     * not regress and prove nothing about all-India or about the fail-closed rule.
     *
     * These differ from the live row ONLY in the city fields, so any disagreement
     * is attributable to the gate rather than to a keyword or a range.
     */
    {
      label: "all-India",
      note: "every Indian label, incl. the two buckets — must EXCLUDE non-india only",
      prefs: { ...prefsRow, all_india: true, selected_cities: [] },
    },
    {
      label: "no city answer",
      note: "neither a selection nor the switch — must match NOTHING, fail-closed",
      prefs: { ...prefsRow, all_india: false, selected_cities: [], cities: [] },
    },
    {
      label: "Mumbai umbrella",
      note: "what ticking 'All Mumbai areas' stores — the save-time expansion, as a filter",
      prefs: {
        ...prefsRow,
        all_india: false,
        selected_cities: ["mumbai", "navi-mumbai", "mumbai-suburban", "thane"],
      },
    },

    /*
     * THE WORK-MODE VARIANTS. The live row ticks all three, which makes the mode
     * gate a no-op for it — so running only the live row would prove the port did
     * not regress and prove nothing whatever about a rule that can now hide a job.
     * Same argument the experience matrix and the city variants make, one field
     * over. These differ from the live row ONLY in work_modes, except the last,
     * which also narrows the cities in order to reach the branch it is named for.
     */
    {
      label: "all modes",
      note: "the live row's own answer, stated explicitly — the mode gate is a no-op",
      prefs: { ...prefsRow, work_modes: ["onsite", "hybrid", "remote"] },
    },
    {
      label: "onsite only",
      note: "the relaxation cannot fire; a NULL work_mode still reads as onsite and stays",
      prefs: { ...prefsRow, work_modes: ["onsite"] },
    },
    {
      label: "remote only",
      note: "only postings a description PROVED remote — every unread row is onsite and drops",
      prefs: { ...prefsRow, work_modes: ["remote"] },
    },
    {
      label: "remote relaxes city",
      note: "remote + one city: a remote India job outside blr must still match",
      prefs: { ...prefsRow, work_modes: ["remote"], all_india: false, selected_cities: ["blr"] },
    },
    {
      label: "no mode answer",
      note: "nothing ticked — must match NOTHING, fail-closed",
      prefs: { ...prefsRow, work_modes: [] },
    },

    /*
     * THE SALARY VARIANTS, AND THEY ARE THE WEAKEST IN THIS MATRIX BY
     * CONSTRUCTION — which is why they are labelled rather than trusted.
     *
     * Every live row is unpriced (salary_min IS NULL on all 9,823), and an
     * unpriced job passes salary_ok whatever is ticked. So ALL THREE of these
     * must return exactly the live row's total, and a matrix in which they did
     * anything else would be reporting a bug rather than coverage. That makes
     * them a regression guard against the gate accidentally consulting a NULL —
     * the failure mode lib/jobs.salaryClause is written to avoid — and nothing
     * more. The synthetic probes below are what actually exercise the rule.
     */
    {
      label: "all bands",
      note: "the live row's own answer, stated explicitly — the pay gate is a no-op",
      prefs: { ...prefsRow, salary_bands: ["<10", "10-15", "15-25", "25-40", "40-50", "50+"] },
    },
    {
      label: "one band only",
      note: "a single narrow band — still the full total, because no live row is priced",
      prefs: { ...prefsRow, salary_bands: ["15-25"] },
    },
    {
      label: "no bands",
      note: "nothing ticked — must match EVERYTHING the live row does, fail-OPEN",
      prefs: { ...prefsRow, salary_bands: [] },
    },
  ];

  /**
   * A variant's position, by its label.
   *
   * Every reference into VARIANTS goes through this. The alternative — literal
   * indices — has already gone wrong once in this file: the fail-closed
   * work-mode assertion read `VARIANTS.length - 1` with a comment explaining
   * that indexing from the END made it safe against insertions ABOVE, and the
   * salary slice then inserted three variants BELOW it, silently re-pointing the
   * assertion at a variant that must behave the opposite way.
   */
  function variantIndex(label: string): number {
    const index = VARIANTS.findIndex((v) => v.label === label);
    if (index === -1) throw new Error(`match-parity: no variant labelled ${label}`);
    return index;
  }

  const pythonMatched = python({ variants: VARIANTS, jobs }).matched;
  let drifted = 0;

  console.log(
    "prefs matrix — the same open rows under twelve filters (experience, city, work mode):",
  );
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

  // The same treatment for the city gate's fail-closed branch, and for the same
  // reason: two implementations agreeing on a wrong answer would pass a comparison.
  // "A blank city answer matches nothing" is the PROPERTY, and a fallback to the
  // legacy column or to the install's three cities would make both sides agree on
  // ~38 instead.
  const noCityAnswer = perVariant[5].size;
  if (noCityAnswer !== 0) {
    console.error(
      `\nA BLANK CITY ANSWER MATCHED ${noCityAnswer} job(s). Neither a selection nor ` +
        `the all-India switch must match nothing — see notify/match.city_ok.`,
    );
    process.exitCode = 1;
  }

  /*
   * A COVERAGE GATE RATHER THAN A NOTE, since the work-mode slice. This line used
   * to print "0 of 7,869 non-NULL" so that a green matrix could not be misread as
   * coverage of a branch no live row could reach. The parser now fills the column,
   * so zero means something different and worse: the backfill has not been applied,
   * or the enrichment rail stopped writing — and the matrix above is once again
   * green over a rule live data cannot reach.
   */
  const byMode = new Map<string, number>();
  for (const job of jobs) {
    const key = job.work_mode ?? "NULL";
    byMode.set(key, (byMode.get(key) ?? 0) + 1);
  }
  const parsedWorkMode = jobs.filter((j) => j.work_mode !== null).length;
  console.log(
    `\nwork_mode: ${parsedWorkMode} of ${jobs.length} rows non-NULL   ` +
      [...byMode]
        .sort((a, b) => b[1] - a[1])
        .map(([m, n]) => `${m}=${n}`)
        .join("  "),
  );
  console.log(
    "  NULL is read as 'onsite' at match time and is never written down — see " +
      "notify.match.DEFAULT_WORK_MODE",
  );
  if (parsedWorkMode === 0) {
    console.error(
      "\nNO ROW CARRIES A work_mode. The remote relaxation cannot fire, so every " +
        "variant above is green over a rule live data cannot reach. Run " +
        "`python scripts/backfill_work_mode.py --dry-run` and read the report.",
    );
    process.exitCode = 1;
  }

  /*
   * THE SALARY COVERAGE LINE IS A REPORT AND NOT A GATE, WHICH IS THE OPPOSITE OF
   * THE work_mode LINE DIRECTLY ABOVE, AND THE ASYMMETRY IS DELIBERATE.
   *
   * Zero non-NULL work_mode means the backfill was never applied — a state
   * somebody must fix, so it exits non-zero. Zero priced rows means Indian job
   * boards do not publish pay, which is the measured and expected state of this
   * board: 36 of 36 sampled candidate descriptions stated no number. Failing on
   * that would be a permanently red check that says nothing, and a check nobody
   * can make green is a check people learn to ignore — including on the day it
   * starts meaning something.
   *
   * So the number is PRINTED, and the synthetic probes below are what hold the
   * rule. If this ever climbs off zero, that is news worth reading rather than an
   * error, and the band spread is what makes it legible.
   */
  const priced = jobs.filter((j) => j.salary_min !== null || j.salary_max !== null);
  console.log(
    `\nsalary: ${priced.length} of ${jobs.length} rows priced` +
      (priced.length
        ? `   ${priced
            .slice(0, 8)
            .map((j) => `${j.salary_min}-${j.salary_max}`)
            .join("  ")}`
        : "   (expected: Indian boards on this watch list do not publish pay)"),
  );
  console.log(
    "  an unpriced job passes the pay gate whatever is ticked, so this being zero " +
      "means the salary variants above are a no-op — see the synthetic probes",
  );

  /*
   * Fail-closed, ASSERTED rather than compared — both languages agreeing on a
   * wrong answer would pass a comparison, exactly as for the blank city answer
   * and the unconfigured range. "Ticking no arrangement matches nothing" is the
   * property.
   *
   * BY NAME, NOT BY INDEX, and the salary slice is why. This used to read
   * `perVariant[VARIANTS.length - 1]` with a comment saying that indexing from
   * the end stopped a variant ADDED ABOVE from re-pointing it — which was true,
   * and silently wrong the moment three variants were added BELOW. It then
   * asserted "matches nothing" about the blank SALARY answer, which must match
   * everything, and the check would have failed for the right reason by pure
   * accident. A lookup by label cannot be re-pointed from either direction.
   */
  const sizeOf = (label: string): number => perVariant[variantIndex(label)].size;

  const noModeAnswer = sizeOf("no mode answer");
  if (noModeAnswer !== 0) {
    console.error(
      `\nA BLANK WORK-MODE ANSWER MATCHED ${noModeAnswer} job(s). Ticking no ` +
        `arrangement must match nothing — see notify.match.work_mode_ok.`,
    );
    process.exitCode = 1;
  }

  /*
   * FAIL-OPEN, ASSERTED IN THE OPPOSITE DIRECTION FROM ALL THREE ABOVE — and the
   * direction IS the assertion. An empty salary_bands must match exactly what the
   * live row matches, because notify.match.salary_ok treats "no bands ticked" as
   * "do not filter on pay" while the city, range and mode rules all treat blank
   * as "match nothing".
   *
   * Asserted as EQUALITY WITH THE LIVE TOTAL rather than as a non-zero count,
   * because a clause that dropped some rows and kept others would pass a
   * "> 0" test. The set identity is what "this filtered nothing" means.
   */
  const liveTotal = perVariant[0].size;
  for (const label of ["all bands", "one band only", "no bands"]) {
    const total = sizeOf(label);
    if (total !== liveTotal) {
      console.error(
        `\nTHE ${label.toUpperCase()} VARIANT MATCHED ${total} job(s), not the ` +
          `live row's ${liveTotal}. Every live row is unpriced and an unpriced job ` +
          `passes the pay gate whatever is ticked — so a difference here means the ` +
          `gate is consulting a NULL. See lib/jobs.salaryClause.`,
      );
      process.exitCode = 1;
    }
  }

  /*
   * THE SYNTHETIC PROBE — the one thing live data cannot prove.
   *
   * The strict prose detector labels ZERO rows of the current board remote (see
   * notify/description._REMOTE_FRAMED_RE for why the threshold is where it is),
   * so every variant above exercises the ONSITE and HYBRID paths and none of them
   * reaches the relaxation. A green matrix would therefore say nothing whatever
   * about the branch this slice exists to turn on, which is exactly the trap the
   * old "0 of 7,869 non-NULL" note was printed to avoid.
   *
   * So four fabricated rows are judged by BOTH implementations. They are not
   * inserted anywhere — they exist for the length of one comparison — and they
   * cover the branch and its two floors: a remote job outside the selection
   * matches, the same job outside INDIA does not, an unparsed job in the same
   * place does not, and an account that did not tick `remote` does not get it.
   */
  const SYNTHETIC: { note: string; job: Row; prefsIndex: number; expect: boolean }[] = [
    {
      note: "remote + chennai, account ticked remote -> MATCHES (the branch fires)",
      job: synthetic("syn-remote-chennai", "chennai", "remote"),
      prefsIndex: 10,
      expect: true,
    },
    {
      note: "remote + non-india -> NO (the country floor still holds)",
      job: synthetic("syn-remote-foreign", NON_INDIA_LABEL_LOCAL, "remote"),
      prefsIndex: 10,
      expect: false,
    },
    {
      note: "unparsed + chennai -> NO (NULL is onsite, not remote)",
      job: synthetic("syn-null-chennai", "chennai", null),
      prefsIndex: 10,
      expect: false,
    },
    {
      note: "remote + chennai, account ticked ONSITE only -> NO",
      job: synthetic("syn-remote-onsite-acct", "chennai", "remote"),
      prefsIndex: 8,
      expect: false,
    },

    /*
     * THE PAY PROBES, and they carry more weight than the remote ones did.
     *
     * The remote branch was unreachable by live data because the PARSER was
     * strict; the pay gate is unreachable because no Indian board on this watch
     * list publishes a number at all, which is not a threshold anybody can tune.
     * So these six fabricated rows are the ONLY evidence in either language that
     * the gate does anything, and the second one is the whole slice: a priced job
     * outside every ticked band must disappear.
     *
     * `prefsIndex` points at "one band only" (15-25) and "no bands", resolved by
     * label below rather than by a literal, because this list has already been
     * re-pointed once by an insertion above it.
     */
    {
      note: "priced 18-24 LPA, account ticked 15-25 -> MATCHES (the ranges overlap)",
      job: synthetic("syn-pay-overlap", "blr", null, ["18", "24"]),
      prefsIndex: variantIndex("one band only"),
      expect: true,
    },
    {
      note: "priced 55-60 LPA, account ticked 15-25 -> NO (the gate is not a no-op)",
      job: synthetic("syn-pay-outside", "blr", null, ["55", "60"]),
      prefsIndex: variantIndex("one band only"),
      expect: false,
    },
    {
      note: "UNPRICED, account ticked 15-25 -> MATCHES (no price always shows)",
      job: synthetic("syn-pay-null", "blr", null, [null, null]),
      prefsIndex: variantIndex("one band only"),
      expect: true,
    },
    {
      note: "priced 12-18 LPA straddling the band floor -> MATCHES (overlap, not containment)",
      job: synthetic("syn-pay-straddle", "blr", null, ["12", "18"]),
      prefsIndex: variantIndex("one band only"),
      expect: true,
    },
    {
      note: "priced 55-60 LPA, account ticked NOTHING -> MATCHES (blank fails OPEN)",
      job: synthetic("syn-pay-blank-bands", "blr", null, ["55", "60"]),
      prefsIndex: variantIndex("no bands"),
      expect: true,
    },
    {
      note: "priced 55-60 LPA, account ticked all six -> MATCHES (the operator's no-op)",
      job: synthetic("syn-pay-all-bands", "blr", null, ["55", "60"]),
      prefsIndex: variantIndex("all bands"),
      expect: true,
    },
  ];

  const synthPy = python({
    variants: SYNTHETIC.map((s) => ({ prefs: VARIANTS[s.prefsIndex].prefs })),
    jobs: SYNTHETIC.map((s) => s.job),
  }).matched;

  console.log("\nsynthetic probe — the remote branch, which no live row can reach:");
  let synthFailed = 0;
  for (const [i, probe] of SYNTHETIC.entries()) {
    const py = synthPy[i].includes(probe.job.canonical_id);
    const ts = isMatch(probe.job, VARIANTS[probe.prefsIndex].prefs);
    const ok = py === ts && ts === probe.expect;
    if (!ok) synthFailed += 1;
    console.log(
      `  ${ok ? "ok  " : "FAIL"}  py ${py ? "MATCH" : "no   "}  ts ${ts ? "MATCH" : "no   "}  ` +
        `expected ${probe.expect ? "MATCH" : "no"}   ${probe.note}`,
    );
  }
  if (synthFailed) {
    console.error(
      `\n${synthFailed} synthetic probe(s) failed. The remote relaxation is not ` +
        `behaving as specified, or the two implementations disagree about it.`,
    );
    process.exitCode = 1;
  }

  // The live row's verdict is what the rest of the report describes: the per-company
  // breakdown, the "For you" total, and the number this script exists to be
  // comparable with (preview_digest.py). The other variants are checked above
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
