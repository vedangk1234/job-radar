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

**`ALLOWED_EMAIL` and `MY_EMAIL` are different jobs**, and both are required.
`ALLOWED_EMAIL` is the sign-in gate: which Google account may sign in. `MY_EMAIL`
is
the identity — since Phase 1b `src/lib/currentUser.ts` looks up
`public.users WHERE lower(email) = lower(MY_EMAIL)`, and every preferences read,
job list and badge is keyed on the id it finds. There is no fallback on that path
(the User-Agent path in `lib/ats.ts` still has one): a wrong or missing value
resolves to nobody, and Home and the preferences page error rather than quietly
showing you somebody else's answer. **It must be set on Vercel**, which never
sees the parent `.env`.

That arrangement is temporary. Phase 2 adds magic-link auth, the user id comes
from the session, and `currentUser.ts` is deleted along with its Python twin
`db/users.py`. It reuses `MY_EMAIL` rather than adding a second identity variable
precisely so the two runtimes cannot disagree about who you are.

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

**The company list is the shared catalog, not your watch list.** Phase 1 made the
watch list per-user in the database (`user_companies`) and stopped there
deliberately; 1f moved the badge and explicitly left this list alone. So both
screens show every catalog company, there is no picker for adding or removing
one, and `scripts/track_companies.py` is the only supported way to create a
tracking row until that screen exists. The Add screen writes `companies` and only
`companies` — which means a company added here is crawled from the next hour and
reaches nobody's digest until it is tracked for somebody.

Whether a job has been emailed is deliberately absent from the Home **filter**.
Home is "what fits me right now", not "what has not been emailed yet" — a job
sent in this morning's digest is still a job worth applying to this afternoon. It
is shown as a **badge** instead, which is the same fact reported rather than
acted on.

### Status badges

One per row, derived at render from data the row already has. There is no column
for any of them and there must not be: a badge is a reading of the match rules
plus one per-user fact, and the match rules depend on the `preferences` row,
which is editable from another tab. A stored badge would be a cached answer to a
question whose inputs change under it.

| Badge | Where | Means |
| --- | --- | --- |
| **NEW** | Home | a match **I** have not been told about — no `user_job_notifications` row for me and this job |
| **NOTIFIED** | Home | a match **I** have been told about — such a row exists |
| **SUGGESTED** | Jobs | this raw-list row also passes the filter |

Non-matching rows on Jobs get no badge at all, which is the point: that screen is
several hundred postings including the sales roles, and SUGGESTED is how the few
that fit are findable in it.

**"Told me", not "told somebody".** The first two badges read `jobs.notified`
until Phase 1f — one boolean on the job, meaning "somebody was emailed about
this". That is a faithful paraphrase of "I was emailed about this" for exactly as
long as there is one account; with two, the first person's digest turns the
second person's NEW into NOTIFIED, asserting an email that person never got. So
`user_notified` now comes from `lib/jobs.notifiedToUser(userId)`, the `EXISTS`
mirror of the predicate the digest queue asks as `NOT EXISTS` in
`notify/store.CANDIDATES_SQL`: the queue asks it to decide what to send, the
badge asks it to decide what to say. The column itself was dropped in 1g, and
this screen being its last reader is why 1f had to come first.

Prove it with `npm run badge-check`: it asserts the column is gone from both the
schema and this codebase, then creates a second account inside a
always-rolled-back transaction and checks the badge differs correctly in both
directions. For the one real account the old and new readings cover the same set
of jobs, which is why nothing on screen moved.

Most matches read NOTIFIED today, because most of the table is back catalogue: a
company's first-ever crawl stores its whole board silently, and the 1d backfill
recorded every job the old boolean covered as already-notified for the one
existing account. NEW is not speculative — since the hourly cron a genuinely new
posting arrives with no row for anybody and reads NEW until that hour's digest
sends. A job **somebody else's** digest sent stays NEW here, which is the whole
point of the change.

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
| `npm run badge-check` | proves the NEW / NOTIFIED badge is per-user, with a second account inside a rolled-back transaction |
| `npm run config-roundtrip` | proves a company added here stores `config` as a jsonb **object**, not a double-encoded string |
| `npm run ui-check` | renders the real job row over live rows from a **running** dev server and asserts the checkbox and badges are actually in the markup |
| `npm run mint-session` | a local test session cookie — see the file header |

## What this app does not do

No cron, no email, no writes to the crawler's Python code. It cannot touch the
lifecycle of a job: the crawler owns `jobs.seeded` and `jobs.status`, and a
dashboard able to edit either could silently un-send or re-send a digest. Nor can
it write `user_companies` or `user_job_notifications` — it reads the second for
the badge and does not touch the first at all, so it cannot subscribe you to a
company and cannot mark a job as sent.

It also never fetches a job description. The `desc not checked` hint on Home
reports that `desc_checked` is still false for that row and stops there —
reading the description happens during the **crawl** (`crawler/enrich.py`, moved
there in Step 5d), so the hint marks the minutes between a job appearing and the
next crawl reaching it. Doing it from a page load would make opening a screen
cost a few hundred network requests.

The writes it performs are `companies.tracked`, `companies.category`, **this
user's** `preferences` row (found by owner since Phase 1b, not at `id = 1`),
inserting a company, and `jobs.applied` / `jobs.applied_at` — see above for why
those last two are the exception that proves the rule.
