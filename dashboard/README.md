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

`notified` is deliberately absent from the Home filter. Home is "what fits me
right now", not "what has not been emailed yet" — a job sent in this morning's
digest is still a job worth applying to this afternoon.

## Scripts

| Command | What it does |
| --- | --- |
| `npm run dev` | dev server on :3000 |
| `npm run build` | production build |
| `npm run typecheck` | `tsc --noEmit` |
| `npm run db:check` | proves the transaction pooler works with `prepare: false` |
| `npm run parity` | diffs the TS ports against the Python originals |
| `npm run match-parity` | diffs the match filter against `notify/match.py` over the live table |
| `npm run mint-session` | a local test session cookie — see the file header |

## What this app does not do

No cron, no email, no writes to the crawler's Python code. The jobs views are
strictly read-only: the crawler owns `jobs`, and a dashboard able to edit
`notified` or `status` could silently un-send or re-send a digest.

It also never fetches a job description. The `desc not checked` hint on Home
reports that `desc_checked` is still false for that row and stops there —
reading the description is the digest run's job, and doing it from a page load
would make opening a screen cost a few hundred network requests.

The only writes it performs are `companies.tracked`, `companies.category`, the
single `preferences` row, and inserting a company.
