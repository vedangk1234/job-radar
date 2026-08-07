import { sql } from "./db";
import { matchCountsByCompany, type CompanyJobCounts } from "./jobs";
import { loadPreferences } from "./preferences";

/**
 * Shared company queries. Here rather than inline in the routes because the
 * page components read them directly too — a server component that renders the
 * companies list has no reason to make an HTTP request to itself.
 */

export type CompanyRow = {
  id: number;
  name: string;
  ats: string;
  token: string;
  category: string | null;
  tracked: boolean;
  india_scoped: boolean;
  config: Record<string, unknown>;
  open_jobs: number;
  total_jobs: number;
};

/**
 * A company row plus the numbers both screens print next to its name.
 *
 *   open_jobs     every open job stored for it — the row's "Total jobs"
 *   match_jobs    how many of those pass the digest filter — the row's "For you"
 *   applied_jobs  how many of THOSE MATCHES are already ticked as applied to
 *
 * The second number is the whole point of Home, and it is computed here rather
 * than in the browser: a count is 2 bytes and the rows behind it are 300 KB.
 *
 * The third rides along for free — it is counted in the same pass over the same
 * candidate rows, so the "3 applied" note beside a company costs no query. It
 * counts matches rather than all open jobs on purpose: it sits next to the "for
 * you" number and has to be a share of it, not a total from a wider population
 * that could exceed the figure printed beside it.
 */
export type CompanyCard = CompanyRow & { match_jobs: number; applied_jobs: number };

/** The company rows and their open/total counts. One aggregate, no job rows. */
async function companyRows(id: number | null = null): Promise<CompanyRow[]> {
  return sql<CompanyRow[]>`
    SELECT c.id,
           c.name,
           c.ats,
           c.token,
           c.category,
           c.tracked,
           c.india_scoped,
           c.config,
           COUNT(j.canonical_id) FILTER (WHERE j.status = 'open')::int AS open_jobs,
           COUNT(j.canonical_id)::int                                  AS total_jobs
      FROM companies c
      LEFT JOIN jobs j ON j.company_id = c.id
     WHERE (${id}::int IS NULL OR c.id = ${id}::int)
     GROUP BY c.id
     ORDER BY c.name ASC
  `;
}

export async function listCompanies(): Promise<CompanyRow[]> {
  return companyRows();
}

/**
 * Every company, with both counts. Two queries and one join in memory, run in
 * parallel: the counts query walks the companies, the match query walks the
 * handful of open jobs in the right cities, and neither waits for the other.
 *
 * Untracked companies are included deliberately. They are not being crawled, so
 * their jobs are as stale as the day tracking was switched off — but they are
 * still the jobs we hold, and silently dropping a company from the list is how
 * you forget you turned it off.
 */
export async function listCompanyCards(userId: string): Promise<CompanyCard[]> {
  const prefs = await loadPreferences(userId);
  const [rows, counts] = await Promise.all([companyRows(), matchCountsByCompany(prefs, userId)]);
  return rows.map((row) => withCounts(row, counts));
}

/** One company, with every count. For the response to a PATCH. */
export async function companyCard(id: number, userId: string): Promise<CompanyCard | null> {
  const prefs = await loadPreferences(userId);
  const [rows, counts] = await Promise.all([companyRows(id), matchCountsByCompany(prefs, userId)]);
  return rows[0] ? withCounts(rows[0], counts) : null;
}

/** A company with no matching jobs is absent from the map, not zero in it. */
function withCounts(row: CompanyRow, counts: Map<number, CompanyJobCounts>): CompanyCard {
  const found = counts.get(row.id);
  return { ...row, match_jobs: found?.match ?? 0, applied_jobs: found?.applied ?? 0 };
}

/**
 * Home's order: the companies with something for you, first, most first; then
 * everything else by name.
 *
 * Sorted here rather than in SQL because match_jobs is not a column — it is the
 * answer to a question SQL cannot ask without restating notify/match.py. Twelve
 * rows sort instantly and the comparator is testable by reading it.
 */
export function byMatchesThenName(a: CompanyCard, b: CompanyCard): number {
  return b.match_jobs - a.match_jobs || a.name.localeCompare(b.name);
}

/**
 * Upsert on (ats, token) — the same unique key and the same conflict rules as
 * scripts/seed_companies.py and scripts/add_workday.py, so adding a company
 * here and adding it from the CLI converge instead of fighting.
 *
 * Two conflict rules are inherited deliberately:
 *   - `category` is hand-tagged once and never stomped by an automatic guess.
 *   - `config` is only overwritten by a NON-empty one, so re-adding a Workday
 *     tenant by name could not blank the host/site it needs to be crawled.
 *
 * `xmax = 0` is the standard trick for telling an INSERT from an UPDATE inside
 * an upsert: a freshly inserted row has no deleting transaction stamped on it.
 * It is what lets the UI say "added" rather than "added or maybe already there".
 *
 * CONFIG IS BOUND WITH sql.json(), AND JSON.stringify() HERE IS A BUG
 * ------------------------------------------------------------------
 * postgres.js picks a serialiser from the parameter's resolved Postgres type,
 * and `::jsonb` resolves this one to jsonb (OID 3802) — for which the serialiser
 * IS JSON.stringify (src/types.js registers the json handler against both 114
 * and 3802). So stringifying first meant the value was encoded twice: the column
 * ended up holding a jsonb *string* whose contents are JSON text, not a jsonb
 * object. `jsonb_typeof` says 'string', and Company.from_row on the Python side
 * decodes it once and gets a `str` back, so `config.get("host")` raises
 * `'str' object has no attribute 'get'` and the crawl trips Guard 1 forever.
 * That is what happened to LSEG, the first company added through this route.
 *
 * It bit the empty config too: `'"{}"'::jsonb` is not `'{}'::jsonb`, so the
 * ON CONFLICT rule below could not recognise "no config was supplied" and a
 * re-add by name would have stomped a Workday row's host/site with a string.
 *
 * sql.json() hands postgres.js the OBJECT and lets it serialise exactly once.
 * Same shape the Python side writes (see scripts/add_workday.py, which passes
 * json.dumps(...) to a `$3::jsonb` asyncpg parameter — asyncpg does no implicit
 * JSON encoding, so there one encode is right and here it is one too many).
 */
export async function upsertCompany(input: {
  name: string;
  ats: string;
  token: string;
  config: Record<string, unknown>;
  category: string;
  indiaScoped: boolean;
}): Promise<{ id: number; inserted: boolean }> {
  // `Record<string, unknown>` is what a parsed request body is worth to the type
  // checker; sql.json wants postgres.js's JSONValue. The value did come out of
  // JSON.parse, so it is one — this asserts that rather than widening the
  // parameter, which would push the same cast onto every caller.
  const config = input.config as Parameters<typeof sql.json>[0];

  const [row] = await sql<{ id: number; inserted: boolean }[]>`
    INSERT INTO companies (name, ats, token, config, category, tracked, india_scoped)
    VALUES (${input.name}, ${input.ats}, ${input.token},
            ${sql.json(config)}::jsonb, ${input.category}, TRUE, ${input.indiaScoped})
    ON CONFLICT (ats, token) DO UPDATE
       SET name         = EXCLUDED.name,
           tracked      = TRUE,
           india_scoped = EXCLUDED.india_scoped,
           config       = CASE WHEN EXCLUDED.config = '{}'::jsonb
                               THEN companies.config ELSE EXCLUDED.config END,
           category     = COALESCE(companies.category, EXCLUDED.category)
    RETURNING id, (xmax = 0) AS inserted
  `;
  return row;
}

/**
 * The one field the dashboard may edit on a company.
 *
 * `category` is a hand-tag — product / gcc / services / staffing / unknown —
 * and changes nothing at all. It is here because the tag is only ever right if
 * it can be fixed from the screen where it looks wrong; the seed scripts
 * deliberately refuse to overwrite it (see upsertCompany), so this is its only
 * editor.
 *
 * `tracked` USED TO BE THE SECOND FIELD, AND REMOVING IT IS A SECURITY FIX
 * ----------------------------------------------------------------------
 * This statement used to carry `tracked = COALESCE($1::boolean, tracked)`, fed
 * by a toggle on every company row of Home and Jobs. That column is not a
 * per-user preference and never was: scripts/crawl.py selects
 * `FROM companies WHERE tracked = TRUE`, so it is the crawler's catalog-wide
 * exclusion axis — one shared row, one shared value, for the whole install.
 *
 * With Phase 2a opening sign-up, the arithmetic changed and the control did not.
 * The route behind it authenticates but authorises nothing beyond "is there a
 * session", and this UPDATE is scoped by company id alone — so any stranger who
 * signed up ninety seconds ago could switch crawling OFF for any company, or for
 * all fifty, and every other account's digest would go quiet with nothing
 * anywhere recording why. A dashboard write that can silence somebody else's
 * inbox is the exact failure the `applied` / `seeded` ownership split in
 * lib/jobs.ts exists to prevent, arriving one table over.
 *
 * IT IS REMOVED RATHER THAN GATED, and that is the load-bearing choice. The
 * obvious alternative — let only the operator call it — needs an operator
 * identity, and there is deliberately none in the deployed app: lib/env.ts has
 * carried no myEmail() since 2b, and the comment there warns that an ambient
 * identity read in src/ becomes the thing the next page in a hurry reaches for.
 * So gating would mean reversing 2b to protect a flag the web app has no
 * business owning. A removed statement cannot be reached by curl at all; a gate
 * is a condition a later edit can get wrong silently.
 *
 * WHAT THAT COSTS, stated rather than glossed: no script in this project sets
 * `tracked = FALSE` — every Python writer sets TRUE (scripts/seed_companies.py,
 * scripts/add_workday.py, crawler/discover.TRACK_SQL, which is ON CONFLICT DO
 * NOTHING precisely so a sweep cannot undo a hand-set FALSE). This toggle was
 * therefore the only off-switch in the repo, and switching a company off is now
 * a statement against the pooler by whoever holds the DSN. That is a rare
 * operator action moving to the runtime that already owns the column.
 *
 * The column is still READ here — companyRows() selects it and the row still
 * shows a tracked/off pill. Reading a crawler fact is not owning it.
 *
 * Optional, and a patch with nothing in it is rejected by the route rather than
 * silently doing nothing. Returns false when no such id exists.
 */
export async function updateCompany(
  id: number,
  patch: { category?: string },
): Promise<boolean> {
  // COALESCE on a NULL parameter: an absent key binds NULL and the column keeps
  // its current value. `category` is nullable in the schema, but "no category"
  // is spelled 'unknown' on this screen, so NULL here can only ever mean "not
  // being edited". It served two fields before `tracked` came out and is kept
  // for the one that remains, so re-adding a second editable column is an
  // addition rather than a rewrite.
  const rows = await sql<{ id: number }[]>`
    UPDATE companies
       SET category = COALESCE(${patch.category ?? null}::text, category)
     WHERE id = ${id}
    RETURNING id
  `;
  return rows.length > 0;
}
