# job-radar dashboard

Next.js App Router + TypeScript. Reads and writes the same Supabase database the
Python crawler uses; changes nothing about the crawler itself.

The two runtimes are deliberately separated: Python at the repo root, this app
in `dashboard/`. The Vercel project's **Root Directory** is `dashboard`, so the
Python package is outside the deployment entirely.

## Running it

```sh
cd dashboard
cp .env.local.example .env.local     # then fill in real values
npm install
npm run db:check                     # proves the pooler works before any page does
npm run dev                          # http://localhost:3000
```

## Auth

Google OAuth via Auth.js v5, locked to one account.

Google's "sign in" authenticates *any* Google account. The lock is the `signIn`
callback in [`src/auth.ts`](src/auth.ts), which refuses to mint a session unless
the verified address equals `ALLOWED_EMAIL`. The check is enforced three times —
at sign-in, in the JWT callback, and again on every request — because a single
point of enforcement is a single point of failure.

Every page and every API route is behind it. `src/middleware.ts` matches
*everything* except the sign-in page, Auth.js's own endpoints and static assets,
so a page added later is protected by default. Each API route additionally calls
`requireSession()` for itself.

Redirect URIs that must exist in the Google Cloud console:

| Environment | URI |
| --- | --- |
| local | `http://localhost:3000/api/auth/callback/google` |
| production | `https://<vercel-domain>/api/auth/callback/google` |

The production one cannot be registered until the Vercel domain exists.

## Database

`DASHBOARD_DB_URL` is the Supabase **transaction** pooler on port 6543, not the
session pooler on 5432 the crawler uses.

Transaction mode returns a connection to the pool after every transaction, so
consecutive statements can land on different backends — which breaks prepared
statements, since those are per-backend state. `src/lib/db.ts` therefore sets
`prepare: false`. This mirrors `statement_cache_size=0` in `db/connection.py`.
`npm run db:check` exercises it with concurrent parameterised queries, which is
the condition under which the bug actually shows up.

## Duplicated logic, and how it is kept honest

`src/lib/ats.ts` and `src/lib/workday.ts` are TypeScript ports of
`fetchers/resolve.py` — the token-variant rules, the alias table, the
greenhouse → lever → ashby order, and `parse_workday_url`. They exist because
there is no Python in the Vercel deployment.

`src/lib/match.ts` is a port of `notify/match.py` for the same reason, and it
matters more: it is what the **For you** count on Home means. A job counted
there is a job the digest email would contain — same cities, same keywords, same
exclusions, same level ceiling, same two description-derived facts.

That is real duplication and it will drift. Two checks stop it:

| Command | Checks |
| --- | --- |
| `npm run parity` | `lib/ats.ts` + `lib/workday.ts` against `fetchers/resolve.py`, over fixed inputs |
| `npm run match-parity` | `lib/match.ts` against `notify/match.py`, over every open row in the live database |

Run the second one after touching either matcher. Its total is directly
comparable to `python scripts/preview_digest.py --dry-run --ignore-notified`.

## The two screens

Home (`/`) and Jobs (`/jobs`) are the same list of collapsible companies —
literally the same component, `src/components/CompanyList.tsx`, with one prop
different. Home passes `mode="matches"`, so a row expands to the jobs that fit
you and carries a **For you** count. Jobs passes `mode="all"`, so a row expands
to every open posting including the sales roles, and is the screen that answers
"did the crawler miss it, or did the filter drop it?".

Neither ships job rows with the page. Both ship counts; a row fetches its own
jobs from `/api/companies/:id/jobs?filter=matches|all` the first time it opens.

`notified` is deliberately absent from the Home **filter**. Home is "what fits me
right now", not "what has not been emailed yet" — a job sent in this morning's
digest is still a job worth applying to this afternoon. It is shown as a
**badge** instead, which is the same flag reported rather than acted on.

### Status badges

One per row, derived at render from data the row already has. There is no column
for any of them and there must not be: a badge is a reading of the match rules
plus `notified`, and the match rules depend on the `preferences` row, which is
editable from another tab. A stored badge would be a cached answer to a question
whose inputs change under it.

| Badge | Where | Means |
| --- | --- | --- |
| **NEW** | Home | a match with `notified = false` — in the digest queue, not yet emailed |
| **NOTIFIED** | Home | a match with `notified = true` — already went out in a digest |
| **SUGGESTED** | Jobs | this raw-list row also passes the filter |

Non-matching rows on Jobs get no badge at all, which is the point: that screen is
several hundred postings including the sales roles, and SUGGESTED is how the few
that fit are findable in it.

Every match in the database is currently `notified = true`, because the table was
silently seeded and the digest cron does not exist yet — so Home reads NOTIFIED
everywhere until the cron starts leaving fresh jobs unsent.

### The Applied checkbox

Home only. Ticking it `PATCH`es `/api/jobs/applied` with the job's
`canonical_id`, which sets `applied = true` and stamps `applied_at`; unticking
clears both. The row then dims, strikes its title through and sinks to the bottom
of its company — the apply link still works, because having applied is the most
likely reason to want to open it again. A small "N applied" count appears beside
the company's "for you" number.

These two columns are the **only** ones on `jobs` the dashboard writes, and the
distinction is ownership rather than read-versus-write. Every other column
records something a fetch observed, so the crawler owns it. These record
something no fetch can discover and no crawl can recompute. The crawler
correspondingly never writes them: `crawler/store.UPSERT_SEEN` and
`notify/store.SAVE_DESCRIPTION_FACTS_SQL` name neither column, and
`tests/test_applied_column.py` asserts that both textually and by ticking a row
and running whole crawl cycles — close, reopen, rename — over it.

## Scripts

| Command | What it does |
| --- | --- |
| `npm run dev` | dev server on :3000 |
| `npm run build` | production build |
| `npm run typecheck` | `tsc --noEmit` |
| `npm run db:check` | proves the transaction pooler works with `prepare: false` |
| `npm run parity` | diffs the TS ports against the Python originals |
| `npm run match-parity` | diffs the match filter against `notify/match.py` over the live table |
| `npm run ui-check` | renders the real job row over live rows from a **running** dev server and asserts the checkbox and badges are actually in the markup |
| `npm run mint-session` | a local test session cookie — see the file header |

## What this app does not do

No cron, no email, no writes to the crawler's Python code. It cannot touch the
lifecycle of a job: the crawler owns `jobs.notified` and `jobs.status`, and a
dashboard able to edit either could silently un-send or re-send a digest.

It also never fetches a job description. The `desc not checked` hint on Home
reports that `desc_checked` is still false for that row and stops there —
reading the description happens during the **crawl** (`crawler/enrich.py`, moved
there in Step 5d), so the hint marks the minutes between a job appearing and the
next crawl reaching it. Doing it from a page load would make opening a screen
cost a few hundred network requests.

The writes it performs are `companies.tracked`, `companies.category`, the single
`preferences` row, inserting a company, and `jobs.applied` / `jobs.applied_at` —
see below for why those last two are the exception that proves the rule.
