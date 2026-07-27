/**
 * Prove the TypeScript ports still agree with the Python originals.
 *
 *     npx tsx scripts/parity-check.ts
 *
 * lib/ats.ts and lib/workday.ts restate rules that already exist in
 * fetchers/resolve.py, because the dashboard deploys to Vercel without Python.
 * Duplicated logic drifts. This runs both implementations over the same inputs
 * and diffs the output, so the drift is a failing check rather than a company
 * that silently resolves to a different board than the crawler would pick.
 *
 * Local-only: it shells out to the repo's Python. It is not part of the Vercel
 * build and does not need to be — it is a check on a commit, not on a deploy.
 */

import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, join, resolve as resolvePath } from "node:path";

import { tokenVariants } from "../src/lib/ats";
import { parseWorkdayUrl } from "../src/lib/workday";

/**
 * Walk up until the Python package appears. Neither `__dirname` nor
 * `import.meta.dirname` is reliably present — tsx transpiles to CJS on this
 * Node version, which kills import.meta — and the answer to "where is the
 * crawler" is a file that either exists or does not.
 */
function findRepoRoot(): string {
  let dir = resolvePath(process.cwd());
  for (let i = 0; i < 6; i++) {
    if (existsSync(join(dir, "fetchers", "resolve.py"))) return dir;
    const parent = dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  throw new Error(
    "could not find fetchers/resolve.py above the working directory — " +
      "run this from inside the repo.",
  );
}

const REPO_ROOT = findRepoRoot();

/** Names chosen to exercise every branch of token_variants. */
const NAMES = [
  "postman",
  "razorpay", // has an ALIAS entry
  "Browser Stack", // space -> both squashed and hyphenated variants
  "CRED",
  "Zeta Suite",
  "Innovaccer, Inc.", // punctuation stripped
  "  Groww  ", // whitespace
  "Café Corp", // non-ASCII, exercises NFKD
  "!!!", // normalises to empty -> no variants at all
];

/** URLs chosen to exercise every branch of parse_workday_url. */
const URLS = [
  "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite",
  "https://adobe.wd5.myworkdayjobs.com/en-US/external_experienced",
  "https://salesforce.wd12.myworkdayjobs.com/en-US/External_Career_Site",
  "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite", // no lang
  "https://nvidia.wd5.myworkdayjobs.com/en-US/Site/job/India-Bangalore/Engineer_JR123", // posting
  "https://acme.wd3.myworkdayjobs.com/wday/cxs/acme/CareerSite/jobs", // API form
  "https://careers.acme.com/en-US/ExternalSite", // vanity domain -> ambiguous
  "nvidia.wd5.myworkdayjobs.com/en-US/Site", // no scheme
  "https://acme.wd1.myworkdayjobs.com/", // no site -> ambiguous
  "",
];

// stdin is read as BYTES and decoded utf-8 explicitly. On Windows, Python's
// text-mode stdin defaults to the ANSI codepage, which mangles a name like
// "Café Corp" into a different string than the one that was sent — and the
// resulting mismatch looks exactly like a real parity failure. Output goes back
// as ensure_ascii JSON, which needs no such care.
const PY = `
import json, sys
sys.path.insert(0, ${JSON.stringify(REPO_ROOT)})
from fetchers.resolve import token_variants, parse_workday_url

payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
print(json.dumps({
    "variants": {n: token_variants(n) for n in payload["names"]},
    "workday":  {u: parse_workday_url(u) for u in payload["urls"]},
}))
`;

function python(): { variants: Record<string, string[]>; workday: Record<string, unknown> } {
  for (const exe of ["python", "python3", "py"]) {
    const run = spawnSync(exe, ["-c", PY], {
      input: JSON.stringify({ names: NAMES, urls: URLS }),
      encoding: "utf8",
      cwd: REPO_ROOT,
    });
    if (run.error || run.status !== 0) {
      if (run.error && "code" in run.error && run.error.code === "ENOENT") continue;
      throw new Error(`${exe} failed:\n${run.stderr || run.error}`);
    }
    return JSON.parse(run.stdout);
  }
  throw new Error("no python interpreter found on PATH");
}

const expected = python();
let failures = 0;

function compare(label: string, input: string, py: unknown, ts: unknown) {
  const a = JSON.stringify(py);
  const b = JSON.stringify(ts);
  if (a === b) {
    console.log(`  ok    ${label}  ${JSON.stringify(input)}`);
    return;
  }
  failures += 1;
  console.log(`  DRIFT ${label}  ${JSON.stringify(input)}`);
  console.log(`        python: ${a}`);
  console.log(`        ts    : ${b}`);
}

console.log("token_variants:");
for (const name of NAMES) compare("variants", name, expected.variants[name], tokenVariants(name));

console.log("\nparse_workday_url:");
for (const url of URLS) {
  // Python omits `ambiguous` when empty and orders keys by construction; the
  // port matches both. Sorting keys here so the diff is about VALUES, not about
  // which order two languages happened to build an object in.
  const sortKeys = (o: unknown) =>
    o && typeof o === "object"
      ? Object.fromEntries(Object.entries(o as object).sort(([x], [y]) => x.localeCompare(y)))
      : o;
  compare("workday", url, sortKeys(expected.workday[url]), sortKeys(parseWorkdayUrl(url)));
}

if (failures) {
  console.error(
    `\n${failures} difference(s). The TypeScript port and fetchers/resolve.py disagree — ` +
      `fix the port, or update both deliberately.`,
  );
  process.exit(1);
}
console.log(`\nOK — ${NAMES.length + URLS.length} inputs, both implementations agree.`);
