# job-radar

A personal, India-scoped job-alert crawler. It watches the ATS boards of a
hand-picked list of companies, stores every posting it finds without judging any
of them, and emails **each user** a digest containing only the roles that are
*genuinely* new to them and match their own tight filter — with a private
dashboard to manage the company catalog, edit the filter, and see what the filter
dropped. "New" means *we* had never seen the posting before, not that a vendor's
date field says so; that distinction is the reason the inbox stays worth opening.

Phase 1 turned the single implicit user into rows: a `public.users` table, a
watch list, a filter and a notification history per person, and a digest that
fans out. The crawl underneath is unchanged and stays user-blind — **crawl once,
fan out**. Auth is still one Google account and one `MY_EMAIL`; the bridge
between them is Phase 2's job to remove.

This file is a **living record of what the project is and how it works**. It
describes the code as it stands, not a specification the code has to obey — when
the code changes, this changes with it. Read it to get oriented; do not treat it
as a constraint on a future design.

---

## Architecture (the three-stage spine)

Every stage is deliberately allowed to know less than the next one.

```
CRAWL                  STORE / DIFF                  NOTIFY
fetch every job        observation-based             filter tightly, per person
no judgment      ->    never trusts source dates  -> email each user exactly once
user-blind             user-blind                    the only user-aware stage
```

1. **Crawl** — fetch the whole board for a company and normalise it to one row
   shape. No filtering happens here at all. (The single exception is Workday,
   which filters to India *at source* for volume reasons; such a company is
   flagged `india_scoped` so the partiality is recorded rather than assumed.)
2. **Store / diff** — reconcile what was fetched against what we stored last
   time. A job is new **iff its `canonical_id` was not already in this company's
   stored state**. Vendor `posted_at` fields are advisory and never decide
   anything: Workday's is the literal string "Posted 3 Days Ago", and Greenhouse
   re-stamps `updated_at` when a recruiter fixes a typo.
3. **Notify** — for each active user in turn: their filter, over the companies
   *they* watch, over the jobs *they* have not been told about; email what
   survives to their own address, and record that it was sent **to them** so
   they are never sent it twice.

The slogan is **store loosely, notify tightly**. `jobs` keeps every posting in
every city forever; a `preferences` row decides what reaches *one* inbox, and
can be changed at any time without losing the history to re-decide against.

### Crawl once, fan out

Stages 1 and 2 are **user-blind and must stay that way**; all the narrowing
happens in stage 3. Phase 1 made that a fact about the schema rather than a
coincidence of there being one account:

```
CRAWL ONCE                          FAN OUT
every tracked board, for            for each active user:
everybody, knows nothing     ->       their preferences row
about users                           their user_companies
                                      their user_job_notifications
                                      -> one email, to their own address
```

Two users watching Adobe is one board fetch an hour, not two. The temptation is
to crawl only what somebody currently watches, and it inverts the design:
seeding is decided from a company's stored row count, so a board that went
unwatched for a month would go dark and then re-alert its entire back catalogue
the day someone added it back. `tests/test_user_companies.py` and
`tests/test_leakage.py` assert the separation rather than leaving it to care —
the first that no crawl-path module so much as names the per-user tables, the
second that nothing of one user's ever appears in another's answer.

### Two runtimes, one database

| | Python crawler | Next.js dashboard |
|---|---|---|
| Location | repo root | `dashboard/` |
| Runs on | GitHub Actions (cron) | Vercel (Root Directory = `dashboard`) |
| Postgres | **session** pooler, port 5432 | **transaction** pooler, port 6543 |
| Driver | asyncpg | postgres.js |

They **never talk to each other** — there is no API between them and no shared
code. Their only contact is the Supabase Postgres database.

Why two different poolers, both mandatory:

- The crawler uses the **session** pooler because asyncpg wants the full
  Postgres protocol. It must not use the direct `db.<ref>.supabase.co` endpoint,
  which is IPv6-only, while GitHub Actions runners are IPv4-only — the scheduled
  crawl would simply never connect. `db/connection.py` warns if the DSN looks
  like the direct endpoint.
- The dashboard uses the **transaction** pooler because a serverless app opens
  and drops connections constantly. Transaction mode returns the connection to
  the pool after every transaction, so consecutive statements can land on
  different backends — which breaks prepared statements, since those are
  per-backend state. Hence `prepare: false` in `dashboard/src/lib/db.ts`,
  mirroring `statement_cache_size=0` in `db/connection.py`. Without it you get
  `prepared statement "s1" does not exist` under concurrency.

The pooler presents a certificate signed by Supabase's own private CA, which is
in no system trust store, so connections are encrypted without verifying the
certificate unless `SUPABASE_CA_CERT` points at the downloaded CA file.

---

## Data model

`db/schema.sql` is idempotent — safe to re-run against a live database. Its
`ALTER TABLE ... IF NOT EXISTS` migrations and guarded `UPDATE`s are written so
that re-applying the schema can never reset a value the dashboard has edited.

### `companies` — the live working set
| Column | Meaning |
|---|---|
| `id`, `name` | surrogate key and display name |
| `ats` | `greenhouse` \| `lever` \| `ashby` \| `smartrecruiters` \| `workable` \| `recruitee` \| `workday` \| `custom_jsonld` \| `custom_sitemap` |
| `token` | board token; for Workday, the tenant |
| `config` | JSONB ATS extras — Workday `{host, site}`, custom `{url}` |
| `category` | `product` \| `gcc` \| `services` \| `staffing` \| `unknown`, hand-tagged |
| `tracked` | `FALSE` excludes the company from crawls entirely |
| `india_scoped` | this company's rows are **deliberately partial** (filtered at source) |

`UNIQUE (ats, token)` is what makes "add a company" idempotent.

### `jobs` — metadata only, **never** descriptions
| Column | Meaning |
|---|---|
| `canonical_id` | primary key. `{ats}:{company_id}:{native_id}`, or `hash:` + sha256 of company + normalised title + location for boards that expose no id |
| `title`, `url` | as published |
| `location` | the vendor's raw string, kept verbatim |
| `city` | computed label, one of 26: 23 cities plus `other-india` \| `non-india` \| `india-unclear`. `TEXT`, no `CHECK` — see *City labelling* |
| `source_posted_at` | advisory only, **never** trusted for "new" |
| `first_seen_at` | the authoritative "new" timestamp — our own observation |
| `last_seen_at` | most recent fetch that contained it |
| `miss_count` | consecutive fetches that did *not* contain it |
| `status` | `open` \| `closed` |
| `seeded` | stored during this company's first-ever crawl — **back catalogue, never emailed to anybody** |
| `work_mode` | `onsite` \| `hybrid` \| `remote`, from the description. **NULL = never established**, read as onsite when filtering and never written down |
| `min_experience` | years the description demands as a minimum. **NULL = unknown; whether unknown means keep is the reader's `show_no_experience_jobs`, per account, since Slice 2** |
| `is_pm_role` | the description frames the role as product/people management |
| `salary_min` / `salary_max` | pay the description states, **normalised to LPA**. NULL = no salary established — read as "unpriced", which SHOWS the job to everyone |
| `salary_checked` | a parser has read this posting's prose looking for pay. **The column that stops the pair repeating `jobs.notified`**: without it, "read it, states no pay" and "nobody has looked" would share one NULL |
| `desc_checked` | the description has been read once; never read twice |
| `desc_fetch_failed` | we looked and could not read it — retried, occasionally |
| `desc_checked_at` | when we last looked. NULL = never |
| `applied` | **dashboard-owned.** I applied to this. The crawler never writes it |
| `applied_at` | when the box was first ticked. NULL whenever `applied` is FALSE |

**There was a `notified` boolean here until Phase 1g, and it was two facts
wearing one name.** "This row is back catalogue" is a property of the **crawl** —
user-blind, permanent, true for everybody forever. "Somebody was emailed this" is
a property of a **person**. While the install had one account the second was an
adequate paraphrase of "I was emailed this"; the moment there are two, the first
user's digest silences the job for the second, who never hears about it at all
and has nothing anywhere recording that they didn't. So the two were separated
rather than renamed:

| The fact | Where it lives now | Written by |
|---|---|---|
| this row is back catalogue | `jobs.seeded` — one column, user-blind | `crawler/store.mark_new`, from the same `seeding` flag `scripts/crawl.py` decides from the company's stored row count |
| this person was told | `user_job_notifications` — one row per (user, job) | `notify/store.record_sent`, after Resend returned an id |

`seeded` is the **silent-seed marker and nothing else**, so a crawl still has no
opinion about anybody's inbox. It joins `first_seen_at` and `canonical_id` as a
write-once column: it is absent from `UPSERT_SEEN`'s `ON CONFLICT` clause, which
is what makes "a re-sighting cannot re-queue a job" a property of the SQL rather
than a rule the write path has to remember.

The order mattered more than the destination. 1d gave the person half an owner,
1e moved the crawl half into `seeded` **and switched the queue onto it**, 1f
moved the dashboard badge across, and only then did 1g drop the column — so the
drop was a genuine no-op against a queue that had been reading `seeded` for a
whole sub-phase, rather than a behaviour change with an inbox-shaped failure
mode. See *The diffing guards* for the queue itself.

The six description-derived columns are the **only** trace a job description
ever leaves in this database. There is no description column, on purpose: two
scalars per job cost nothing, and 10 KB of HTML per job across a growing board
is how a free tier turns into a bill. The discipline is structural rather than a
habit — there is nowhere to put the text.

`applied` / `applied_at` are the exception to "the crawler owns `jobs`", and the
exception is about **ownership rather than about writing**. Every other column
here records something a fetch observed, so the thing that fetched it is the only
thing entitled to say what it is. These record something no fetch can discover
and no crawl can recompute — so the dashboard writes them and the crawler never
does. That is a property of the SQL, not a promise: `crawler/store.UPSERT_SEEN`
names neither column in its INSERT list nor in its `ON CONFLICT DO UPDATE`
clause, and `notify/store.SAVE_DESCRIPTION_FACTS_SQL` names neither either, so a
re-sighting, a rename, a close and a reopen all leave the tick alone.
`tests/test_applied_column.py` asserts that twice — against the statement text,
and by ticking a row and running whole crawl cycles over it.

`desc_fetch_failed` exists because `desc_checked` used to conflate two states:
"read it, no minimum stated" and "asked, got nothing". Both leave
`min_experience` NULL and both mean keep, but only the second is worth asking
about again. `desc_checked_at` is what makes that retry occasional (once a day)
rather than once per crawl.

### `discovered_companies` — everything the sweep looked at
Written by `crawler/discover.py` (driven by `scripts/run_discovery.py`):
`job_count`, `last_posting_at`,
`city_hits`, `looks_like_services`, `source`, `promoted`, `UNIQUE (ats, token)`.

It is the **audit log of the discovery sweep, not a queue** — a company that
cleared both bars is already in `companies` and is recorded here with
`promoted = TRUE` as well. What makes the table worth reading is the rest: the
boards that answered but had no job in the three cities, and the names no board
answered for at all.

Two conventions the schema does not state:

- `ats` and `token` are `NOT NULL`, so a name that resolved to nothing is
  logged under the placeholder ATS `unresolved`, keyed by its normalised name.
  That row is what stops the next run spending thirty requests on it again.
- `promoted` is OR-ed rather than assigned on re-run, so a row promoted by hand
  between two sweeps stays promoted even if its board has since gone quiet.

`config` carries the free-text extras the table has no column for: the candidate
name as typed, the city spread, a probe error, and the `"try Workday"` note.

### `notifications`
The sent-log: `canonical_id`, `sent_at`, `digest` (e.g. `2026-07-25-am`). A
receipt per email, so a job is never emailed twice. It is **not** the thing that
decides — `user_job_notifications` is — and the two are written in one
transaction; see below.

### `public.users` — the account, made explicit
Columns: `id` (`UUID`, `DEFAULT gen_random_uuid()`), `email` (`TEXT NOT NULL
UNIQUE`), `created_at`, `plan` (`TEXT`, default `free`, no `CHECK` — matching
`companies.category`), `is_active` (`BOOLEAN`, default TRUE). Plus
`users_email_lower_idx`, a unique index on `lower(email)`.

**The anchor the rest of Phase 1 hangs off.** Until 1a the single user was
implicit — one preferences row, one `ALLOWED_EMAIL`, one `MY_EMAIL` recipient,
and nothing in the database naming any of them. Everything per-user added since
is a foreign key back to this table.

**Why `UUID` and not the `SERIAL` the rest of the schema uses.** Phase 2 adds
magic-link auth, and both Supabase Auth (`auth.users.id`) and the Auth.js
database adapters key users by uuid. A `SERIAL` here would mean carrying a
separate provider-id column to join on forever. `gen_random_uuid()` is built into
Postgres 13+, so this needs no extension on Supabase.

**Why it is the one schema-qualified table in the file.** Supabase ships its own
`auth.users`, so `users` is the single ambiguous table name on this database, and
a bare `CREATE TABLE IF NOT EXISTS` resolves against the connection's
`search_path` — which `db/connection.py` does not set, making the target a
server-side default this project does not own. The failure that guards against is
silent, not loud: with `auth` ahead of `public` on the path, `IF NOT EXISTS`
would find `auth.users` already there, skip, report success, and leave
`public.users` uncreated. **Every statement naming this table, in both runtimes,
says `public.users`.**

**Why the lowercase index is separate from the `UNIQUE`.** Email identity is
case-insensitive in practice and `TEXT UNIQUE` is not, so `Me@example.com` and
`me@example.com` would be two accounts — and under Phase 2 magic links, two
accounts a person cannot tell apart. The plain `UNIQUE` stays because it is what
`ON CONFLICT (email)` targets; the index is what makes the identity
one-per-person. Both runtimes compare `lower(email)` on both sides to match it.

**No seed row, and this is the one table where the schema cannot supply one.**
Every other seed in `db/schema.sql` is a literal; this one is a real person's
address, and "no hardcoded personal data, anywhere" is enforced rather than
requested — the pre-commit hook would block the `INSERT`. `scripts/seed_user.py`
is the seed and reads `MY_EMAIL`, exactly as `notify/digest.py` does. Apply the
schema, run that, and the anchor row is back: a fresh database is reproducible
without the repo ever holding the address.

`is_active` is the account-level off switch, and it sat unread from 1a until 1e.
`db/users.DIGEST_USERS_SQL` is what reads it: a deactivated account keeps its
preferences, its watch list and its notification history and stops receiving
email. Deleting the row would also stop the email and take all three with it,
which is a different decision and one nobody can undo.

### `preferences` — the filter, as data rather than code
Columns: `cities`, `keywords`, `exclusions` (all `TEXT[]`), `max_level` (`INT`,
default 2), `updated_at`, and `user_id` (`UUID`, FK to `public.users`). Phase 3
Step 0 added nine more; five are wired up, four are still inert:

| Column | Status |
|---|---|
| `onboarding_completed` | **live** since Slice 1 — the gate `requirePage()` reads |
| `exp_range_low`, `exp_range_high` | **live** since Slice 2 — the looking range. Nullable, and NULL means *not configured*, which matches nothing |
| `show_no_experience_jobs` | **live** since Slice 2 — what a NULL `jobs.min_experience` means for this account |
| `years_of_experience_held` | stored, editable, **read by no matcher, ever** — informational only |
| `selected_cities`, `all_india` | **live** since cities Slice B — the city gate. `cities` is now the legacy fallback for a row that predates the backfill, and the contract slice drops it |
| `work_modes` | **live** since the work-mode slice — the arrangement filter. Blank matches nothing |
| `salary_bands` | **live** since the salary slice — the pay filter, and the one field here that is **fail-OPEN**: blank means "do not filter on pay", not "match nothing" |

It lives in the database and not in a Python constant precisely so the dashboard
can `UPDATE` it and the very next digest obeys — no redeploy, no commit.
`notify/match.py` carries `DEFAULT_*` constants that mirror the seed for tests
and un-migrated databases; the database is the runtime source of truth and the
constants are never an override.

**The row has an owner, not a fixed address.** It used to be found at `id = 1` —
`id INT PRIMARY KEY DEFAULT 1 CHECK (id = 1)` — and every loader in both
runtimes said so. Phase 1b replaced that with `WHERE user_id = <current user>`;
`preferences_one_per_user_idx` (a *partial unique index*, chosen so named
multi-profiles stay one `DROP INDEX` away) enforces one row per user.

The `CHECK (id = 1)` outlived 1b and came down in **1e**, which is exactly the
sub-phase the schema comment predicted would need it: a fan-out with one filter
is not a fan-out, and two users sharing a filter would not test isolation at all
— it is the second *filter* that proves a digest was computed for a person rather
than for the install. So the `CHECK` and the fixed `DEFAULT 1` went (an
unqualified `INSERT` would otherwise collide with the row already there) and `id`
got a real sequence, spelled out as the three statements `SERIAL` is sugar for so
each stays independently idempotent. `preferences_one_per_user_idx` is untouched
by that: the cap came off the **table**, not off the rule about a person, and
named multi-profiles are still one `DROP INDEX` away rather than accidentally
opened.

**Whose row? The MY_EMAIL bridge, and it is temporary.** Auth is Phase 2, so
until then both runtimes resolve "the current user" the same way: look up
`public.users WHERE lower(email) = lower(MY_EMAIL)`, once per run. That is
`db/users.py` (`current_user_email`, `resolve_current_user_id`) on the Python
side and `dashboard/src/lib/currentUser.ts` on the TypeScript side. It reuses
the address the install already had rather than adding a `DEFAULT_USER_ID`,
because two variables that must agree about who you are can disagree silently.
Both files are marked for deletion when magic-link auth lands. Note the
asymmetry: the TS half memoises (a Next server has no single entrypoint), the
Python half does not (its entrypoints resolve once and thread the id down).

**The bridge is not how the fan-out finds people, and that distinction is what
keeps it disposable.** `db/users.digest_users` reads every `is_active` row and
`run_fan_out` passes each id down explicitly, so the send path never asks who is
running the script. The bridge answers a different question — "whose dashboard is
this, whose preview is this" — which is precisely the question a Phase-2 session
answers instead. Deleting these two files therefore removes a lookup, not a
capability.

**The nullable window.** `user_id` is nullable, because `ADD COLUMN … NOT NULL`
would need a default the schema cannot know — the UUID is resolved from the
environment. So applying `db/schema.sql` and running
`scripts/backfill_preferences_user.py` is **one unit, with no crawl or digest
between them**. In that window a user-scoped loader finds nothing, and both
loaders tell that apart from a genuinely absent row (one extra query on the miss
path) and **raise** rather than falling back to the seeded defaults — silently
emailing the results of a filter nobody chose is exactly the failure the crawl
guards exist to prevent.

### `user_companies` — the watch list, per person
Columns: `id` (`SERIAL`), `user_id` (`UUID` → `public.users`), `company_id`
(`INT` → `companies`), `created_at`, `UNIQUE (user_id, company_id)`, plus
`user_companies_company_idx` on `company_id` for the reverse lookup.

**The catalog stays shared and is crawled exactly once for everybody.** Two
users watching Adobe is one board fetch an hour, not two. This table is the
schema behind "crawl once, fan out" — and only the *fan out* half reads it.

**The crawl path has never heard of it, and that is asserted rather than
promised.** `scripts/crawl.py` still selects `FROM companies WHERE tracked =
TRUE` and nothing else. A join here would make crawling conditional on somebody
currently wanting the results, which inverts the design: seeding is decided from
a company's stored row count, so a board that went unwatched for a month would go
dark and then re-alert its entire back catalogue the day someone added it.
`tests/test_user_companies.py` checks the absence three ways — the statement text
of `COMPANIES_SQL`, the source text of every crawl-path module, and behaviourally
(three companies in the catalog, one watched, all three still selected).

Both foreign keys are `ON DELETE CASCADE`, which is one argument said twice: a
tracking row is a statement about a pair, and if either half is gone the row
asserts nothing and cannot be read back by any query that joins to what it names.
The two key types differ deliberately — `UUID` because `public.users.id` is,
plain `INT` because `companies.id` is a `SERIAL`, exactly as `jobs.company_id`
already declares.

`companies.tracked` is a **different axis** and this table does not read it.
`tracked = FALSE` means "excluded from crawls entirely", a property of the
catalog; a row here means "this person wants alerts for this company", a property
of a person. They agree today and answer different questions.

That distinction stopped being merely conceptual in Phase 3 Slice 1a. The
dashboard used to let any signed-in account write `companies.tracked` from a
toggle on every company row — a per-install flag edited through a control that
looked like a personal setting, which is precisely the confusion this paragraph
warns about, shipped. The write is gone (see *Writes the dashboard is allowed to
make*), and `companies.tracked` is still sealed.

**The web app writes this table since the watch-list slice, and it is the only
per-user table it writes.** `dashboard/src/lib/watchlist.ts` is the writer —
`watchCompany` / `unwatchCompany`, reached from `POST` / `DELETE /api/watchlist`
and from the picker on `/watchlist` and `/onboarding`. It is a **port of
`db/watchlist.track_companies`**, not a second design: the two statements are
compared normalised, in both directions, by `tests/test_watchlist_write.py`,
because there is no API between the runtimes and a Vercel request cannot call
Python. The same file seals the table to that one module — no other file under
`dashboard/src` may write `user_companies` or `user_job_notifications`, for the
reason the bulk `INSERT` was deleted in 1e.

`db/watchlist.py` holds the statements (so the tests exercise what production
runs) and `scripts/backfill_user_companies.py` is the seed — the same MY_EMAIL
bridge as the preferences backfill, tracking every catalog company for the single
user so behaviour is unchanged. Its success gate is **coverage of a snapshot
taken before the insert**, not a fresh re-count: the discovery sweep can add a
company mid-run, and a gate that re-counted the catalog would fail a migration
that did its job perfectly. A company that appears mid-run is named in the output
and picked up by the next run.

**Creating a pair has two halves, since 1e.** A tracking row does not only say
"send me this company's jobs" — it also decides what counts as this person's
history. The queue is "open, in a company you watch, not back catalogue, not yet
sent to you", so the instant the row exists every job that company already has is
in that person's queue, and their first digest is an archive dump. `jobs.seeded`
does not cover this: it stops a company's **first-ever crawl** reaching anyone and
says nothing about the weeks of ordinary crawling after it, which is the door
history walks through. So `db/watchlist.track_companies` writes the pair **and**
records that company's stored jobs as already-notified for that user, in one
transaction — there is no instant in which the pair exists and the suppression
does not, and the hourly digest runs in exactly such an instant if the process
dies there.

Two sharp edges are worth keeping in mind. The suppression runs **only on a
genuine insert** (`RETURNING` against `ON CONFLICT DO NOTHING`), because running
it over an already-tracked pair would swallow every job that arrived since —
somebody's actual queue, silently. And there is now **one statement and one
function** for creating a tracking row: the bulk `INSERT ... SELECT` that tracked
the whole catalog in 1c was deleted rather than kept-but-unused, because a
convenient path that skips the second obligation is a loaded gun aimed at
whichever future caller reaches for it. `scripts/track_companies.py` is the
command-line form and takes `--email` rather than `MY_EMAIL`, unlike every
migration script here — it acts on a *named* person, and defaulting the target to
whoever the environment belongs to is how you quietly configure your own account.

### `user_job_notifications` — "this person has been told about this job"
Columns: `id` (`SERIAL`), `user_id` (`UUID` → `public.users`), `canonical_id`
(`TEXT` → `jobs`), `notified_at`, `UNIQUE (user_id, canonical_id)`, plus
`user_job_notifications_job_idx` on `canonical_id`.

**A row existing is the entire decision.** This user has been told about this
job; no row means not yet. There is no flag on it and no state in between, which
is the difference from the boolean it replaced — there is nothing to misread.

**The job side is `canonical_id`, not a `job_id`, because `jobs` has no `id`.**
The primary key there is `canonical_id TEXT`. `notifications` already spells the
key exactly this way, and a second name for the same key on a sibling table is
how a join gets written wrong once and reviewed as correct forever. So this table
carries `TEXT` against the `UUID` on the other side — two deliberately different
key types on one table, each being what the thing it references actually is,
exactly as `user_companies` carries `UUID` against `INT`.

**Both foreign keys are `ON DELETE CASCADE`**, the same argument the other two
per-user tables make: a receipt that outlives its owner records that nobody was
told something, which is not data. The job side has a sharper version — the
fan-out is an *anti-join* of `jobs` against these rows, so an orphan is a row no
join can ever return, and the table's count and the join's count would disagree
for reasons nobody can see.

**`notified_at` is record-keeping and nothing branches on it**, deliberately. It
means "when this state was recorded", which for the 1d backfill is the migration
itself: for the ~7,700 rows the silent seed had marked notified we do not know
when, or whether, an email ever went out — `notifications` holds fifteen receipts
against them. Back-dating from `first_seen_at` would put an invented send date in
a column, which is the mistake `source_posted_at` exists to name. So it follows
`notifications.sent_at` (written, never read to decide) and deliberately **not**
`desc_checked_at`, which *does* branch. A future "re-notify after N days" has to
argue with this paragraph first, because the moment anything branches on the
value, "when recorded" and "when sent" stop being interchangeable and the
backfilled rows start lying.

**Two kinds of row, and neither is a lie.** Most are receipts written by
`record_sent` after a real send. The rest were written because the job must
*never* be emailed to that person — the 1d backfill of everything the old boolean
covered, and the tracking-time history suppression above. Both say the identical
thing to the only query that reads them: this person will not be told about this
job. What they do not share is a `notifications` receipt, and that is honest —
one was sent, the other never will be.

**The silent seed is deliberately not handled here.** It is a property of a crawl
rather than of a person, so it lives in `jobs.seeded` and the queue reads it
directly. Handling it here instead would mean one row per user per seeded job —
6,496 of them from a single afternoon's discovery sweep, times everyone who ever
signs up.

`db/notifications.py` holds every statement that **writes** this table and
nothing else. The digest's own read is not there: it is four lines of predicate
inside one query joining `jobs` to `companies`, and splitting that across two
modules to isolate them would make the query harder to read than the coupling it
removed. It lives in `notify/store.CANDIDATES_SQL`.

---

## The diffing guards (why the inbox stays trustworthy)

The expensive failure of this tool is not missing a job — it is crying wolf. An
inbox that has once shown eighty jobs you already saw is an inbox you stop
opening, and then the tool is worthless even when it is right. So every guard is
biased the same way: **when the data is doubtful, change nothing and say so.**

`crawler/diff.py` is pure set arithmetic over three inputs (`known_open`,
`known_closed`, `fetched`) producing three disjoint sets (`truly_new`,
`reappeared`, `missing`). `reappeared` collapses "still open" and "closed and
back" into one set because both mean the same thing for alerting: we already
decided about this, and a flap must never read as news.

The guards live in `crawler/loop.py` and run in this order:

- **Guard 1 — a fetch error changes nothing.** A 500 is not evidence that a
  board is empty. Were an exception allowed to reach the diff, every open job
  would land in `missing`, three bad days would close all of them, and the
  recovery day would re-alert the entire board as new.
- **Guard 2 — a partial fetch changes nothing.** If a board returns fewer than
  `PARTIAL_FETCH_FLOOR` (0.5) of the known-open count, the fetch is refused on
  shape alone. This is the one that actually bites: a paginator dying mid-way
  returns a valid, well-formed, wrong answer with *no error at all*, and nothing
  downstream can tell 40-of-100 from a company that cut 60 reqs. The floor is
  deliberately loose because real boards do shed a third of their reqs in a
  freeze, and a guard that fires on ordinary churn gets switched off. It is also
  skipped when nothing is known open yet, so a first crawl can still seed.
- **Guard 3 — close only after `MISS_LIMIT` (3) consecutive misses.** Absence is
  a single observation, not a verdict. `missing` is computed against
  `known_open` only, so an already-closed job that stays absent is not "missing
  again" and cannot push `miss_count` up forever.

Guards 1 and 2 `return` before any write, so "it changed nothing" is structural
rather than something the write path has to remember.

**The silent seed.** On a company's first-ever crawl its whole back catalogue
looks new. A seeding run therefore stores everything with `seeded = TRUE`, so
nothing is ever emailed for it **to anybody**; real alerting starts on crawl #2.
`seeding` is decided by the caller from the company's stored row count and
changes exactly one thing — that flag. Being a property of the crawl, it excludes
the same rows for every user forever, which is what "the silent seed is silent
for all of us" means.

Nothing is emailed from the crawl loop. Brand-new jobs are simply left
`seeded = FALSE`, which makes them *eligible* for a queue — but eligibility is
not the queue, and since the fan-out it takes two more clauses to become one.

**What the digest actually reads** (`notify/store.CANDIDATES_SQL`), three clauses
answering three different questions:

```sql
 WHERE j.status = 'open'
   AND EXISTS     (user_companies uc WHERE uc.user_id = $1
                                       AND uc.company_id = j.company_id)
   AND j.seeded = FALSE
   AND NOT EXISTS (user_job_notifications n WHERE n.user_id = $1
                                              AND n.canonical_id = j.canonical_id)
```

| Clause | Asks | Whose fact |
|---|---|---|
| `EXISTS user_companies` | is this job's company one *you* asked about? | a person's — the watch list |
| `j.seeded = FALSE` | is this job back catalogue? | a crawl's — user-blind and permanent |
| `NOT EXISTS user_job_notifications` | have *you* been told about it? | a person's — since 1d |

Per-user in the two places that decide, user-blind in the one that describes a
crawl. The middle clause sits in exactly the position `j.notified = FALSE`
occupied before it, and that is the whole reason the swap was safe: the old
boolean was never one rule. It meant "not seeded **and** not emailed", and only
the second half is about a person — which is why a job emailed to me silenced it
for everybody else. The two halves were separated rather than swapped.

`EXISTS` rather than a join to `user_companies` for a reason the `NOT EXISTS`
already gives: the question is existence, Postgres reads both as a semi-join
anyway, and a join invites the reader to wonder whether the row can come back
twice. It cannot — `UNIQUE (user_id, company_id)` — but `EXISTS` says so without
being asked. And note what the queue deliberately does *not* ask:
`companies.tracked`. That is the crawl's exclusion axis, a property of the
catalog; conflating it with a watch list would mean a dashboard toggle silently
emptying somebody else's digest.

---

## Fetchers / ATS support

Every fetcher returns a list of dicts with exactly these keys, from
`fetchers/normalize.make_job`:

```
{"native_id", "title", "location", "url", "posted_at", "department"}
```

A missing vendor field becomes `None` — never a crash, never an invented value.
`fetchers/normalize.py` applies no judgment about which jobs matter; it labels a
location, it does not decide whether the label is interesting.

- **Tier 1** (`fetchers/tier1.py`) — `greenhouse`, `lever`, `ashby`,
  `smartrecruiters`, `workable`, `recruitee`. One public JSON endpoint each,
  whole board, no source-side filtering.
- **Tier 2 — Workday** (`fetchers/workday.py`). See below.
- **Tier 3** (`fetchers/tier3.py`) — `custom_jsonld` (scrape `JobPosting`
  JSON-LD) and `custom_sitemap`, for companies on no supported ATS. These expose
  no vendor id, so they get the `hash:` form of `canonical_id`.

Nine ATSes are supported; the live watch list currently uses five of them —
greenhouse 28, smartrecruiters 8, lever 6, workday 5, ashby 3. `workable`,
`recruitee` and both Tier-3 custom routes have **no tracked company at all**
right now, which is worth knowing before trusting any of them: they are exercised
by fixtures and by the discovery probe, not by the hourly crawl. The Tier-3 pair
is the one where that gap bites, because it is also the pair with no description
route (see *Enrichment*).

`fetchers/http.py` provides the shared politeness: one client, a global
semaphore, per-host pacing, and 429/5xx backoff. Its User-Agent carries a
contact address read from `MY_EMAIL`, falling back to `unknown@example.com`.

Two modules in `fetchers/` fetch something other than a board.
`fetchers/resolve.py` guesses a board token from a company name (and takes a
pasted Workday URL apart), and `fetchers/discovery.py` mines GitHub for company
names and board tokens. Both go through the same `polite_get`; the GitHub search
endpoints ask for a slower pace than the shared default, because their
unauthenticated budget is ten requests a minute. See **Discovery** below.

### Workday, in more detail

`POST https://{host}/wday/cxs/{tenant}/{site}/jobs` with
`{"appliedFacets": {...}, "limit": 20, "offset": 0}` →
`{"total", "jobPostings", "facets"}`. It is the only fetcher that filters at
source, because a tenant may carry 2000+ reqs worldwide against ~200 in India
and fetching the whole board would mean 100+ paged POSTs per crawl against a
vendor known to block. `limit=100` is rejected with a 400 by every tenant
tested; 20 is the cap. Workday also takes a slower per-host pace (2.0s) than the
1.0s default.

### Vendor quirks learned the hard way

- **Lever** — `createdAt` is epoch **milliseconds**. `iso_or_none` distinguishes
  seconds from milliseconds by magnitude.
- **Workable** — answers **200 with an empty `jobs` array** for accounts that do
  not exist, so a bare 200 never proves a real board; only a non-empty array
  does. It also rate-limits aggressively.
- **SmartRecruiters** — paginated; stops on `totalFound`, on an empty page, or
  at a hard page cap.
- **Workday: the country facet is not called the same thing twice.** One tenant
  exposes `locationCountry`, another has no country facet at all and carries
  India as a value of `locationHierarchy1`, a third uses a 90-character
  custom-field name. So the facet is discovered **by value** ("which facet has a
  value whose descriptor is exactly India?"), never by name.
- **Workday: never substring-match "india".** One tenant's `locations` facet
  lists "Indiana - Indianapolis" and friends; a contains-check would silently
  scope a company to Indiana, USA. Matching is on an exact descriptor.
- **Workday: an unknown facet value is ignored, not rejected.** Posting a
  country WID a tenant does not know answers 200 with 545 San Jose jobs — a
  near-unfiltered board wearing the costume of a filtered one. So the hardcoded
  `INDIA_WID` fallback is applied, then *checked for having actually narrowed
  anything*, and dropped if it did not.
- **Workday: offset past `total` wraps to page 1.** Asking for offset=10000 of
  212 returns the same twenty rows as offset=0, so a "stop when the page is
  empty" loop would never stop. Paging stops on `offset >= total`, on a page cap,
  and on a page contributing no new ids — three independent stops, because the
  natural one does not exist.
- **Workday: `locationsText` is sometimes a count, not a place** — "2 Locations",
  "3 Locations" on ~26% of one tenant's India rows. Those would label `other` and
  drop out of the funnel, so the `externalPath`'s own location segment is folded
  in and the row stays labellable.
- **Workday failure semantics** (the Workable lesson): `total=0` on a well-formed
  response is a real empty board and returns `[]`. A 404, an HTML error page, or
  a response missing `total`/`jobPostings` **raises**, so Guard 1 leaves the jobs
  table alone. An error is never allowed to look like "every job closed".

---

## Filtering (how a job becomes a match)

`notify/match.py` is the only place filtering happens. It is pure — no database,
no clock, no environment — so the whole notify decision is testable without a
connection.

```
is_match  =  city_ok                     (YOUR cities, or all of India)
         AND keyword_ok
         AND NOT excluded            (a word in the title)
         AND NOT level_excluded      (a level notation in the title)
         AND NOT is_pm_role          (a fact from the description)
         AND experience_ok           (the row's minimum vs YOUR range)
         AND salary_ok               (the row's pay vs YOUR bands — unpriced always shows)
```

The first four are the **title stage** (`title_stage_ok`) — free, and run over
every open job. The last three read columns, not text, and the seam is named
explicitly because the enrichment pass needs to know which jobs are worth a
network request: the ones that already pass every title test.

**The salary rule is the one filter here that fails OPEN**, and it does so twice
over — a job with no stated pay is shown to everyone, and an account that ticked
no bands filters on nothing. Both directions are deliberate and both are argued
in `notify/match.salary_ok`; the short version is that the job side is silent on
essentially the whole board, so "no opinion about pay" is this filter's resting
state rather than an unanswered question. It is therefore also the one onboarding
column `PUT /api/preferences` does **not** require, which means **"onboarded
implies configured" does not extend to `salary_bands`** — unlike the city answer,
the experience range and `work_modes`, where that implication is what makes the
fail-closed branches unreachable.

**The experience rule is per-account since Phase 3 Slice 2**, and it is three
branches rather than one comparison:

```
exp_range_low IS NULL OR exp_range_high IS NULL   ->  match NOTHING
min_experience IS NULL                            ->  keep iff show_no_experience_jobs
otherwise                                         ->  keep iff low <= min_experience <= high
```

It replaced a module constant, `MAX_MIN_EXPERIENCE = 2`, whose own comment
predicted this: *"if it ever needs to be editable it becomes a column, exactly
like max_level did."* The constant is deleted in both languages rather than left
unread — the `jobs.notified` lesson applied to a name.

Two things changed in kind, not degree. The rule can now drop a posting for being
too **junior**, which a ceiling could never express; and a NULL `min_experience`
is no longer unconditionally kept. **The install's own row is `(0, 2)` with
`show_no_experience_jobs = TRUE`, which reproduces the old rule exactly** —
`min_experience` is never negative, so `{0,1,2}` and "at most 2" select the same
rows — and `scripts/backfill_onboarding_prefs.py` seeded it that way for
precisely this step. Verified rather than argued: `match-parity` reports the same
38 matches before and after.

**Only the minimum is stored, and that cost is accepted deliberately.** There is
no `max_experience` column, so a "3–6 years" posting is a `min = 3` row and an
account looking for 5–8 never sees it although 5 and 6 are inside the posting's
own band. The alternative (`min_experience <= high`) would keep it and would never
fire the junior half at all. For a range of `(0, 2)` the two agree on every stored
row; the cost is borne only by an account looking higher up.

**An unconfigured range matches nothing, and is not a fallback to `(0, 2)`.**
Handing an account the operator's answer is the failure Slices 1b and 1c spent two
commits removing from `keywords`, `cities`, `exclusions` and `max_level`. It is
also unreachable for an onboarded account by construction: `PUT
/api/preferences` refuses an incomplete range and is the only writer of these
columns *and* of `onboarding_completed`, so **onboarded implies configured** —
asserted against the live database by `npm run onboarding-check`. It excludes
rather than *raises* because `run_fan_out` catches only `DigestError`, so an
exception would take every account's digest down over one account's blank row.

**Every other ambiguity still resolves toward keeping the job — except the city.**
`min_experience IS NULL` covers three different situations — never read, read and
no minimum stated, and fetch failed — and all three still mean *we do not know*.
What changed is only who answers what to do about not knowing. On the live
database **7,809 of 7,880 open rows are NULL**, so that one boolean decides 99% of
the board — which is why the onboarding form pre-checks it ON (a suggested answer;
the column stays FALSE until a real save) and why the hint on the screen states
the number. A detected PM role still hides a posting outright, and a title stating
no level still survives: treating silence as senior would empty the digest.

`years_of_experience_held` is **informational and never filters.** It is on the
form and in the row, and it is deliberately absent from both `Preferences` value
objects and from `PREFERENCES_SQL`, so "it never decides" is a property rather
than a promise — `tests/test_phase3_columns.py` asserts the name appears in no
matcher in either language.

`city_ok` is the one test with no inclusion bias, and the asymmetry is the
point. A description can be absent and a level can go unstated, but a location
string is always present and always readable — so a label outside the selection
is an answer rather than a gap, and a NULL label is a bug rather than a doubt.
`or ""` turns an unlabelled row into the empty string, which is in no selection,
so "we never labelled it" can never read as a pass.

**Since cities Slice B the gate is three branches over two columns**, and it is
the one filter here that reads what a person TICKED rather than a list they typed:

```
remote posting, or all_india  ->  anywhere except non-india
otherwise                     ->  membership of selected_cities, minus two labels
```

The two relaxing branches share a line deliberately: "I do not care which city"
and "this job has no city" are different questions with the same answer, and
writing the country floor once is what stops it being enforced in one and
forgotten in the other. All-India is stated as the **complement** of `non-india`
rather than as the 25-item list it equals, for the reason `india_funnel` is —
a hand-written set of Indian labels works at 26 and becomes a trap at 27.

**The fail-closed half.** `india-unclear` and `non-india` can never match an
explicit selection (`CITY_SELECTION_NEVER_MATCHES`). The first means "we could not
tell where this job is" — a statement about our confidence rather than about a
location — so a person who ticked `chennai` does not get it, and the all-India
switch, which says the country is enough, does. The second is the label all-India
excludes by definition, and a selection able to match it would mean the narrow
filter admitting rows the wide one refuses. `other-india` is deliberately NOT on
that list: it is a confident statement that the posting is in India at a named
place outside the 23, so ticking it is coherent. The rule is enforced three times
— the picker does not offer them, `PUT /api/preferences` refuses them, and the
matcher re-checks the label, which is what makes it a property rather than a
promise for a row edited by hand.

**A blank city answer matches nothing.** Neither a selection nor the switch is the
brand-new account, and it is the same fail-closed choice the unconfigured
experience range gets — and unreachable for an onboarded account by the same
construction, since `PUT` refuses it and is the only writer of these columns and
of `onboarding_completed`.

**`preferences.cities` is still written and no longer decides.** It is the legacy
column, kept for exactly one slice as the fallback `effective_cities` reads when
`selected_cities` is empty — a row that predates the backfill has its answer there
and reading only the new column would silently empty that account's digest. It is
a fallback to *this* account's own previous answer, not to somebody else's, which
is what distinguishes it from the seeded-defaults failure Slices 1b and 1c
removed. The contract slice stops writing it and drops it.

The explicit branch is still **vocabulary-blind**, which is why growing the label
set from 5 to 26 changed neither matcher: it is a plain membership test over
opaque strings on both sides, and the SQL half reads the same array. The two
excluded labels are the single exception, named once in each language.

**Word boundaries, not substrings.** Every term compiles to a `\b`-anchored
regex. The naive `term in title.lower()` is wrong in both directions and fails
silently: `go` is inside "Goa" and "Django", `api` inside "Rapid" and "Capital",
`lead` inside "Already", `qa` inside "Qatar". A substring exclusion on `qa`
would drop every Bangalore backend role at a company with "Qatar" in the
location line and nobody would ever notice. Multi-word terms join their tokens
with `[\s\-_/]+`, so one pattern covers "back end", "back-end", "back_end" and
"back/end". A term whose edge is a non-word character (`c++`, `.net`) falls back
to a plain escaped match, since `\b` would assert the opposite of what is wanted.

**Levels need their own test** because the word exclusions catch "senior" and
"lead" but miss the other half of how seniority is written: "SDE III",
"Engineer IV", "Backend Engineer L4", "Engineer (Level 5)". `detect_level` reads
four notations — roman numerals, `L3`, `level 3`, and a digit attached to a role
token or trailing the title — and the maximum wins, because "SDE II / III" is a
III posting.

That is the one filter with a real false-positive problem, because titles are
full of numbers that mean the opposite thing: "Ops Engineer 2 to 5 Years",
"Backend Engineer, 3+ years", "Python Developer (2-4 years)". Dropping those
would silently delete exactly the mid-level jobs the filter exists to find. So
`_mask_experience_numbers` blanks every number that denotes experience —
same-length replacement, so every offset and every `\b` still lines up — and
only what survives the mask may be read as a level. The bias is deliberate and
one-directional: an unrecognised level costs one line in a digest, a misread
experience range costs the job.

**Preferences are DB-backed and dashboard-editable**: `keywords`, `exclusions`,
`max_level`, since Slice 2 `exp_range_low` / `exp_range_high` /
`show_no_experience_jobs` / `years_of_experience_held`, and since cities Slice B
`selected_cities` / `all_india`. The API rejects an empty city answer, an empty
`keywords` list and an incomplete experience range, because each of the three would
match no job at all. It also refuses a body carrying **both** a city selection and
`all_india` — the two are alternatives, and silently dropping half of a body that
states both is how a filter gets quietly rewritten. `exclusions` may be empty (a real choice) and so
may `years_of_experience_held` (it filters nothing).

### Enrichment

`crawler/enrich.py` runs **during the crawl**, once per cycle, over title-stage
matches only — a few dozen requests rather than one per row on the board. It
fetches each candidate's description exactly once for as long as the posting
exists, reduces it to `min_experience` and `is_pm_role`, writes those plus the
three bookkeeping flags, and discards the text.

It used to run only in the digest path, which reads a send queue — so a
description was first read when a job was first considered for an *email*. The
dashboard's Home screen asks a different question (everything open that fits,
emailed or not), so a freshly-discovered job appeared there with nothing known
about its experience requirement and was therefore kept. That is correct
behaviour on incomplete evidence; the fix was the evidence, not the rule. One
crawl cycle is now the worst case for how long a job can be shown with its
experience unknown.

Enrichment must never touch a lifecycle column, and that is a property of the
SQL rather than a promise: the `UPDATE` in `notify/store.py` names only the five
description columns. No `seeded`, no `status`, no `first_seen_at` — so it cannot
re-open a closed job, cannot hand a back catalogue to a digest, and cannot make a
job look newly seen. That is what makes it safe to run in the middle of a crawl.

`fetch_enrichment_targets` correspondingly filters on **no** notification state
of any kind, in either the old global form or the per-user one. It is the
*crawl's* queue, not a digest's: every open job that fits has to have had its
description read, whether or not anybody has been emailed about it.

**One user-shaped seam does exist here, and it is worth knowing about.**
`enrich_matches` narrows the pass to title-stage matches, which needs *a*
preferences row — and being a crawl entrypoint it resolves one through the
MY_EMAIL bridge. So today enrichment is scoped to the bridge user's filter, not
to the union of everybody's. With one real account the two are identical; with a
second account whose keywords differ, that user's matches could reach their
digest with `desc_checked = FALSE`, which means `min_experience` NULL, which
means **keep** — the same shape as the Step 7c leak, arriving by a different
route. It is listed under *Pending* rather than fixed here because the fix is a
design choice (union of active filters, or enrich per user) that Phase 2's auth
work will re-frame.

**Where the description comes from**, per ATS — and the split is one request
versus zero:

| Route | ATSes | Cost |
|---|---|---|
| Already in the board payload the crawler fetched | `lever`, `ashby`, `workable` (`details=true`), `recruitee` | 0 extra requests; one board fetch cached per `(ats, token)` per process |
| A single-posting endpoint | `greenhouse` (`?content=true&questions=true`), `smartrecruiters` (public posting API), `workday` (the tenant's own CXS detail path) | 1 request per candidate |
| No verified route | `custom_jsonld`, `custom_sitemap` | none — recorded as a **failed** read, so it is asked again tomorrow rather than believed forever |

An ATS with no route is not a neutral gap, and treating it as one is how jobs
leak. `min_experience` stays NULL, NULL means keep, and the posting goes into the
digest with nothing known about it — so the whole second-stage filter is silently
switched off for that vendor. SmartRecruiters was in this state until 2026-07-28
and it cost 12 of 46 matches; Workday was in it before that. `desc_fetch_failed`
exists so the difference between "read it, it said nothing" and "never managed to
read it" is visible in the table instead of being a thing someone has to remember.

### City labelling

`fetchers/normalize.city_label` returns one of **26 labels** — 23 cities and
three buckets:

```
blr  hyd  pune                                    the three legacy spellings
mumbai  navi-mumbai  mumbai-suburban  thane       the Mumbai region
new-delhi  gurugram  noida                        the NCR
chennai  kolkata  ahmedabad  indore  nagpur  nashik
chhatrapati-sambhajinagar  coimbatore  madurai
hubballi  mangaluru  mysuru  belagavi
other-india   non-india   india-unclear           the buckets
```

Precedence: a named city wins, then a **named Indian place that is not one of
the 23** (`other-india`), then a country/state/region signal that names no city
(`india-unclear`), then a confident non-India anchor (`non-india`), then
vagueness — "Remote", "Multiple Locations" — which resolves to `india-unclear`.
An empty location is `india-unclear`; an unlabelled posting on an Indian
company's board is more likely Indian than not.

`fetchers/normalize.CITY_LABELS` is the vocabulary as a tuple, mirrored in
`dashboard/src/lib/labels.CITY_LABELS`. The runtimes share no code, so the list
exists twice; `tests/test_city_vocabulary.py` parses the TypeScript file and
asserts the two are identical, in the same order. A label one side can produce
that the other rejects is a filter that silently matches nothing.

Matching is generous on purpose: a missed Bangalore posting is a job never seen,
a false positive is one line of noise. When a string is **ambiguous** the bias
is `india-unclear`, because that label can stay in a funnel and a confident one
cannot.

**Two conflations were removed, one at a time, and each had leaked something.**

The first was *ambiguous* versus *Indian, elsewhere*. Every India signal used to
share one bucket, so "Lucknow" and "Remote" got the same label — even though one
is a confident statement that the posting is not in a city we want and the other
is no statement at all. With `india-unclear` in `preferences.cities`, an InMobi
"SDE 2" in Lucknow read as a match, and so did Noida, Mohali and Gurgaon rows:
14 of 61.

The second was *Indian, elsewhere* versus *not India*, and it lasted until the
25-city rewrite. `city_label` collapsed three different verdicts into one label
called `other`: an Indian city we had not asked for, a confidently foreign
posting, and a string no rule recognised. One label, three facts — fine while
the only question anyone asks is "is this one of my three cities?", and fatal
the moment somebody asks "show me everything in India", because `other` can
answer neither yes nor no to it. `_classify` had always computed the
distinction; the rewrite stopped throwing it away one line before returning it.

**The three legacy spellings were kept, and that is what made the rewrite safe
to run against live data.** `blr`, `hyd` and `pune` are what every account's
`preferences.cities` holds, in both runtimes, plus `notify.match.DEFAULT_CITIES`
and the `db/schema.sql` seed. Renaming them to `bengaluru`/`hyderabad` would
have meant rewriting every filter inside the same transaction as the relabel,
with a window in which a half-applied migration empties every digest. Leaving
them alone instead makes something more valuable true: they are scanned **first**
in `_CITY_MARKERS`, they lost no marker, and they gained none that moves a
stored row — so no string that labelled one of them can be stolen by one of the
twenty new cities, and no new label appears in any preferences row. The only
rows the vocabulary can move are rows that were already outside every filter.
Proven rather than argued: over all 1,654 distinct location strings, **0 rows
left those three labels and 0 entered them**; the live pass relabelled 9,171
rows and `npm run match-parity` returned 38 before and after, as did
`preview_digest.py` (blr 33 / hyd 3 / pune 2).

Precedence carries a second rule. A label whose marker **contains** another
label's must be scanned first: "Navi Mumbai" contains the token `mumbai`, so
navi-mumbai, mumbai-suburban and thane sit above mumbai. Otherwise every Navi
Mumbai posting would label `mumbai` and the distinction the city dropdown needs
would be destroyed at write time, where no query can recover it.

**Which states count as ambiguous is relative to the city list, and six changed
sides.** "Is this state ambiguous?" means "does it contain a city somebody can
ask for?" — so Karnataka, Maharashtra and Telangana were the only three while
the vocabulary was three cities, and Tamil Nadu (Chennai, Coimbatore, Madurai),
Uttar Pradesh (Noida), Gujarat (Ahmedabad), West Bengal (Kolkata), Haryana
(Gurugram) and Madhya Pradesh (Indore) joined them when it grew. This file used
to say *"'Uttar Pradesh' cannot be [a target city], and is `other`"*; that was
true of a three-city vocabulary and is false of this one. The flip only bites
when the state is **all** the string says — "Kalyani, WEST BENGAL" names a town,
and the other-india table answers first.

`india_funnel` is the one call site that needs "is this plausibly India at all?"
rather than "which city is this?" — the Workday **unfiltered fallback**, which
trims an unscoped tenant's board before storing it. It is now defined as a
**complement**: everything except `non-india`. It used to be membership of a
hand-written set of Indian labels, which worked at five labels and becomes a
trap at twenty-six — adding a 24th city and forgetting to add it to that set
would make the crawler silently stop **storing** that city, jobs vanishing
before they are ever written down. There is exactly one label the question
answers no to, so that is the one the function names.

**Every marker is in the file because a real posting carries it.** The canonical
modern name of each of the 23 cities is always present — it is the label's own
name, not a guess about spelling. Everything else (an old name, a variant, a
misspelling, an airport code) had to be found by
`scripts/survey_all_locations.py` and carries the row count it was found with:

- **Office/airport codes** — `blr`, `hyd`, `pnq`, `ind pun`/`in pun`. One Workday
  tenant writes every Bangalore posting as "IND-BLR-<building>" (178 rows), which
  matched nothing and labelled `other`: **135 real Bengaluru jobs were being
  discarded by a string format.** A three-letter token is safe because `norm()`
  has already turned every hyphen, comma and slash into a space and `_hit`
  anchors on word boundaries. `ind pun` is anchored as a whole phrase so it
  matches "IND-PUN-Kharadi" and **not** "IND-Punjab-Mohali".
- **Misspellings** — `banglore` (3 rows, PhonePe) and `hyderbad` (5 rows,
  Commvault). The second is worth being honest about: all five of its rows read
  "Bangalore, India; Hyderbad, India", so `blr` outranks it and it relabels
  nothing today. It is in because the spelling is attested and the next posting
  may not carry Bangalore alongside.
- **Post-`norm` forms** — `zu rich`, `sa o paulo`, `que bec`, `mu nchen`. `norm`
  runs NFKD and then strips everything outside `\w\s`, and a combining accent is
  not a word character, so it becomes a **space** rather than vanishing. The
  markers are written in the only form `_hit` will ever see, which is the same
  reason `ind pun` is spelled that way.

Deliberately **rejected**, each after being checked against the data: `beng` and
bare `pun`; `bengalore`/`bangalor`/`banaglore`; **`hubli`, `mangalore`,
`belgaum`** (real alternative names, named in the brief for the rewrite, and
carried by zero rows — the same case, and the same answer); and **all fourteen
airport codes for the new cities** (`bom`, `del`, `maa`, `ccu`, `amd`, `idr`,
`nag`, …), which no board here writes and several of which are words. The
nearest trap in the data is real: "THA Bangkok One Bangkok". Anchoring whole
non-word tokens is what makes a "bang" prefix rule unable to relabel three Thai
jobs as Bangalore.

The one pair attested from somewhere other than the jobs table is
`indiana`/`indianapolis`, and the rewrite is what made them matter. A Workday
tenant's `locations` facet lists "Indiana - Indianapolis" — the reason `_hit`
anchors at all — and the no-marker fallback moved from `other`, which the old
funnel read as foreign, to `india-unclear`, which the funnel keeps. Without
those two markers an unscoped tenant would have started storing its Indiana reqs.

**`ncr` was a marker and it was a bug.** It sat in the Indian table on the
assumption that NCR means Delhi's National Capital Region. The Philippines has
one too, and LSEG writes it: "Makati City, NCR, Philippines" (11 rows), "Makati,
NCR" (2), "Taguig, NCR" (2). All fifteen classified as Indian. Under the old
collapse that was invisible — they landed in `other` beside genuinely foreign
rows — and **the split is what would have made it visible**, by turning them into
`other-india`. The marker is gone and `philippines` answers instead.

`scripts/survey_all_locations.py` is what answers "which spellings deserve a
marker?" from the data instead of from English. It prints every distinct
location string with the verdict it gets **and the marker that produced it**, a
token-frequency view, a fuzzy near-miss pass, and — the one that gates a
migration — the **residue**: strings no marker in any table matched, which are
therefore labelled by a fallback rather than by a rule. `normalize.explain_label`
is what both it and the relabel ask, so a survey cannot certify a residue the
relabel then refuses. (`scripts/survey_locations.py`, the older and narrower
script, surveys only the two dropped buckets and is still the right tool for
that question.)

`scripts/relabel_cities.py` recomputes `jobs.city` for stored rows, and a
vocabulary change does not reach the table without it. It hashes every column
*except* `city` before and after and asserts they are identical, so "the UPDATE
only touches city" is evidence rather than an argument. Since the rewrite it
also reports, and **refuses to apply on**, two more things:

| Gate | Refuses when | Because |
|---|---|---|
| residue | any row's label came from the fallback | nobody can review a verdict with no rule behind it |
| blast radius | a job crosses any user's `preferences.cities` | a job moving **into** a filter has no `user_job_notifications` row, so nothing suppresses it and it goes out in the next digest as news, because a labeller changed |

The apply path checks the same two gates the dry run reports, so "I read the
report and approved it" and "the script agreed" cannot come apart.

### Re-deciding what a description said

`scripts/recheck_descriptions.py` is `relabel_cities.py`'s argument applied to
the other pair of read-once columns, and it is shaped differently because the
evidence is gone. `city` can be recomputed from the location string still on the
row; `min_experience` and `is_pm_role` cannot, because the description was
deliberately discarded. So the script computes nothing — it clears
`desc_checked` on the rows a detector change could newly decide differently
(open, read cleanly, no minimum found, *and* still a title-stage match), and the
next ordinary crawl does the fetching through the ordinary polite path.

It is **not idempotent**, unlike the relabel, and that is the one thing to know
before running it: most descriptions state no minimum, so a row re-read
successfully returns to exactly the state the script selects on. It is a manual
step taken once after `notify/description.py` actually changes — never on the
crawl path, never in the cron.

---

## Discovery — how a company gets onto the watch list

Everything above assumes a watch list exists. For the first six steps that list
was hand-typed. Discovery is the machine that grows it, and it is **two modules,
split along the line between a verdict and a sweep**:

- `crawler/discover.py` — **one candidate in, one verdict out.** It resolves a
  name to a board, applies the two bars, and writes the row. Both of the "never"
  guarantees below are statements about the two SQL constants here (`TRACK_SQL`,
  `LOG_SQL`), not about the script that calls them.
- `scripts/run_discovery.py` — the **sweep**: collect candidate names from every
  source, dedupe them against what is already known, run them through
  `crawler.discover` concurrently, write every ten, and print the report.

The split is why the guarantees are testable. A rule enforced inside the thing
that writes one row holds however that row is reached — from the sweep, from
`--seed-file`, from `--recheck`, or from a future caller that does not exist yet.

```
NAMES                    PROBE                        VERDICT
seed list ─┐
seed file ─┼─ dedupe ─▶  resolve name -> board  ─▶   both bars   -> companies (tracked)
github    ─┘             fetch board, label cities   one bar     -> discovered_companies
                                                     neither     -> discovered_companies
```

**The two bars, and nothing else.** A candidate is auto-tracked iff (a) it
resolves to one of the six Tier-1 boards, and (b) that board has at least one
open job labelled `blr`, `hyd` or `pune`. No name heuristic, no size threshold,
no allowlist. `looks_like_services` is *recorded* rather than acted on — a
staffing firm with real Bangalore engineering reqs clears both bars like anyone
else, and the flag is there so a person reading the table knows why a board is
full of client-site postings.

**Why the city test happens before tracking rather than after.** It is the only
filtering decision in this project that runs ahead of storage, and it looks like
a violation of *store loosely, notify tightly*. It is not: that rule is about
which **jobs** are worth keeping, and this is about which **boards** are worth an
hourly request. A hundred boards with no Indian presence would be a hundred
fetches an hour and several thousand rows of San Francisco job history, to drop
all of it at the last step. The bar is applied once, at the door, against the
board the sweep has just fetched — using the same `city_label` the crawler will
apply an hour later, so the two cannot disagree about what it found.

### Where the names come from

| Source | Module | Yield |
|---|---|---|
| Seed list | `scripts/discovery_seed.py` | ~210 hand-typed names, in two sections |
| GitHub | `fetchers/discovery.py` | board tokens mined out of README job tables |
| `--seed-file` | any text file, one name per line | whatever you drop in, no code change |

The seed list's two sections differ in exactly one way. `ENTERPRISE_AND_GCC` are
the tech-park tenants — Manyata, Bagmane, Embassy, Hitec City, Hinjewadi — and
almost none of them resolve, because an enterprise that size is on Workday,
Taleo or SuccessFactors. Membership there earns an unresolved name a
`"try Workday"` note in `discovered_companies`, so the manual `add_workday.py`
queue writes itself. **The sweep never attempts Workday**: a tenant, a pod host
and a site name are three independent unguessable strings, and guessing them is
how you get a companies row that fails every hour forever.

GitHub mining is not what it first looks like. The obvious query — code search
for `boards.greenhouse.io` — is **401 without a token**, flatly. The
unauthenticated fallback is repository search with `in:readme`, and its top hits
are the community job-list repos ("new grad 2020", "companies sponsoring
visas"), whose READMEs are tables of hundreds of careers links. So the second
step reads those READMEs from `raw.githubusercontent.com` — a different rate
budget from the 60/hour API core limit — and pulls board tokens straight out of
the URLs. A token lifted from `jobs.lever.co/acme` is not a guess that has to
survive `token_variants`; it is the board's real name, and it costs **one**
request to confirm instead of thirty. `GITHUB_TOKEN` is honoured when set and
never required.

### What the sweep is not allowed to do

- **Never writes `jobs`.** A newly-tracked company has zero stored rows, so the
  next `crawl.py` detects a first-ever crawl and seeds its whole back catalogue
  with `seeded = TRUE` — the same silent seed every other company got. That is
  the entire reason a run that tracks forty boards does not produce forty
  boards' worth of email, and `tests/test_discovery.py` asserts it twice: once
  against the statement text, once by counting rows after a track.
- **Never writes `user_companies`.** "Tracked" here means *in the catalog and
  crawled*, which since 1c is a different axis from *somebody wants alerts about
  it*. A swept-in company is therefore stored, diffed and enriched from the next
  hour onwards, and appears in nobody's digest until a `user_companies` row
  exists for it — `scripts/track_companies.py` is what creates one. That is the
  correct default rather than an oversight (auto-subscribing every account to
  whatever a sweep found is the archive-dump failure with a different trigger),
  but it *is* a step that now has to be taken deliberately. See *Pending*.
- **Never `DO UPDATE` on `companies`.** `crawler/discover.TRACK_SQL` is
  `ON CONFLICT DO NOTHING`.
  An existing row knows things the sweep does not — a corrected name, a
  hand-tagged category, a Workday config, or the fact that a person set
  `tracked = FALSE` on purpose. `DO UPDATE` would undo that decision on every
  run, and "the board still has Bangalore jobs" was never the thing in dispute.
  `RETURNING id` therefore yields a row only on a genuine insert, which is
  exactly the "newly tracked" count the report prints.
- **Never stores descriptions**, and never re-fetches a board it has already
  read: `probe_boards` hands back the jobs of the board it settled on, so the
  city test costs no extra request.

### Resolve order: two constants, on purpose

`fetchers/resolve.py` now carries `RESOLVE_ORDER` (greenhouse → lever → ashby)
and `DISCOVERY_ORDER`, which appends smartrecruiters, workable and recruitee.
The wide one is a strict **prefix-preserving superset**, so a name that resolves
to greenhouse for the dashboard resolves to greenhouse here too — discovery can
find boards the "add by name" flow would miss, never a *different* board for the
same name. They are two constants rather than one because `RESOLVE_ORDER` is
ported to TypeScript for a runtime with no Python and a request budget measured
in seconds; discovery runs from a laptop and can afford three more probes per
token. `resolve()` is now a thin wrapper over `probe_boards`, so the shared half
is genuinely shared rather than restated.

### Why the sweep probes 24 candidates at once

Not because 24 is fast — `fetchers/http.py` caps in-flight requests at 8 and
paces every host to one request a second, and nothing in the sweep can raise
either. It is about keeping the **six host lanes busy**, and the first version
got that wrong in a way worth writing down.

It probed in lockstep batches of ten. Ten candidates all walk the vendor list in
the same order at the same time, so ten requests pile onto the Greenhouse lane
while Lever, Ashby, SmartRecruiters, Workable and Recruitee sit idle — then all
ten move to Lever together. Six lanes, one of them working, and the barrier at
the end of each batch meant the slowest candidate held up nine others.
Measured: **2 candidates/minute**.

Queueing every candidate at once behind a semaphore and recording results in
completion order lets them pipeline instead — one is on its fourth vendor while
another starts its first, so every lane has somebody waiting on it. Same
politeness, same request count, same code path per candidate. Measured after the
change: **6 candidates/minute**, and the write-every-ten is now a reporting
interval rather than a barrier.

### Resuming

Interrupt it and run it again. Every candidate already in `companies` or already
in `discovered_companies` is skipped without a request, matched on the
normalised name (and on `(ats, token)` when the source supplied one). Results are
written **every ten candidates rather than at the end** — a sweep that only wrote
on a clean exit would have nothing to resume from. `--recheck` re-probes the
logged near-misses, which is the flag worth running monthly: a board with no
Bangalore job in July may have one in August.

---

## Dashboard

Next.js App Router + TypeScript in `dashboard/`. Read-mostly.

- **Home (`/`)** — per-company collapsible rows showing **matches only**, over
  **the companies this account watches** (since the watch-list slice; it was the
  whole shared catalog before), with a "for you" count per company and, once
  there is one, an "N applied" count beside it. Whether a job has been emailed is deliberately *not* part of this
  filter, in either its old global form or the per-user one: Home answers "what
  fits me right now", not "what has not been emailed yet" — a job sent in this
  morning's digest is still worth applying to this afternoon. It is shown as a
  badge instead. Every job row carries an **Applied** checkbox.
- **Watchlist (`/watchlist`)** — the **company picker**: browse and search the
  full shared catalog, add or remove companies from **your** `user_companies`
  rows. The write path the app never had. Adding one records that company's
  stored jobs as already-seen for you in the same transaction, and the screen
  says how many. A free account may watch **5**; the limit is enforced in
  `lib/watchlist.watchCompany`, inside that transaction, and the picker only
  greys the buttons.
- **Jobs (`/jobs`)** — literally the same component with one prop different
  (`mode="all"`), showing every open posting unfiltered, over the same watched
  companies Home lists. This is the screen that
  answers "did the crawler miss it, or did the filter drop it?". In this mode
  each job additionally shows its `status` and a SUGGESTED badge if it also
  passes the filter. There is no Applied checkbox here: this screen is the raw
  reference, and the tick belongs on the list of jobs worth applying to.
- **Add (`/add`)** — resolve a company **by name** (guess the board token from
  the name, trying mechanical variants against greenhouse → lever → ashby, first
  non-empty board wins) or **by URL** (including Workday, parsed for
  host/tenant/site). It writes `companies` and **only** `companies`: as with the
  discovery sweep, adding a board to the catalog is not the same act as somebody
  subscribing to it, and no `user_companies` row is created here yet.
- **Preferences (`/preferences`)** — edits **this user's** `preferences` row,
  found by owner since 1b rather than at `id = 1`. `PUT` rather than `PATCH`,
  because the editor sends the full lists it is showing. Since the reskin it is
  also the **only** screen carrying `exclusions` and `max_level`, and it wears the
  same designed skin as /onboarding (the editor is one component) while keeping
  the app nav, its own copy and the delete-account section.
- **Onboarding (`/onboarding`)** — the same `WatchlistPicker` and the same
  `PreferencesEditor` (wrapped by `OnboardingSetup`, which carries one number
  between them), with setup copy and no delete-account section, and the one page
  `requirePage()` lets past its own gate. It redirects to Home the moment
  `onboarding_completed` is TRUE, so an onboarded account cannot sit on a setup
  screen. **A save is refused while the account watches no company** — see *The
  onboarding gate*. Since the onboarding-form slice it is **one scrolling page of
  named sections** (resume and keywords → experience → cities → work mode → pay →
  companies) with a **sticky save bar** naming every answer still missing, and
  **the app tabs are hidden on this route** — Sign out is not. Since the reskin it
  wears the design surface in `app/setup.css`, opens on a *Welcome — one-time
  setup* header, ends on a **success screen** rather than an immediate redirect,
  and does **not** show `exclusions` or `max_level` — both still travel in the
  save, and both are edited on /preferences.

### The onboarding gate

A signed-in account whose `preferences.onboarding_completed` is FALSE is
redirected to `/onboarding` instead of the page it asked for. The check is in
`lib/guard.requirePage()` — not middleware, which holds no database connection
and deliberately asks only "is there a verified user" — and it reads
`lib/preferences.onboardingCompleted()`, one lookup on
`preferences_one_per_user_idx`, on page renders only.

It is **inside `requirePage` rather than beside it**, so a page added next month
is gated by default: the same allow-nothing-with-named-holes direction the
middleware matcher chose, where forgetting to register something cannot open a
hole. Three callers pass `{ allowUnfinishedOnboarding: true }` and
`tests/test_dashboard_auth.py` fails on a fourth — `/onboarding` (gated, it would
redirect to itself), `/preferences` (**because deleting an account lives there,
and erasure is a right that does not wait on a filter**) and
`deleteAccountAction` (the same argument for the form post). Exempting
`/preferences` opens no way round the gate: the flag is set by a save, and a save
there is the same editor writing the same row through the same statement.

**Since the watch-list slice, finishing onboarding also requires watching at
least one company.** `PUT /api/preferences` refuses the save otherwise, which
makes it the fifth member of the family that already holds the city answer, the
keyword list, the experience range and the work arrangement — and the one the
queue asks *first*: `CANDIDATES_SQL` opens with `EXISTS (user_companies …)`, so
an account watching nothing matches nothing however good its keywords are. Before
the picker existed **every** account that completed onboarding was in exactly
that state, which is what the slice fixes. The alternative — auto-watching the
catalog for a new account — is the archive dump with a friendly face and the
seeded-defaults mistake Slices 1b and 1c removed from the preferences row, so the
answer is fail-closed, as it is for the city selection. The editor greys Save and
says which answer is missing; `/preferences` has no picker on it, so the message
there links to `/watchlist`.

**API routes are deliberately not gated.** `PUT /api/preferences` is the only
exit from onboarding, so a second exemption list would have its first mistake
seal the only door; the data routes are already scoped to the caller's own id.
`POST /api/watchlist` is deliberately in that ungated set for the same reason it
has to be: an account mid-onboarding has to be able to pick companies, and every
statement behind it is bound to the caller's own `userId`.
Sign-in, the callback and sign-out are untouched by construction rather than by
exemption — none of them calls a guard.

**The company list on Home and Jobs is the watch list, and the catalog moved to
its own screen.** Phase 1 made the watch list per-user in the database and
stopped there on purpose — 1f moved the badge and explicitly left the company
list alone — so for two phases both screens showed every catalog company while
the digest read `user_companies`, and a company could be visible on Home, crawled
hourly, full of matching jobs, and in nobody's digest. The watch-list slice closed
that: `lib/companies.listWatchedCompanyCards` is what both pages call, and
`/watchlist` browses the full catalog.

**Which of the two lists a screen should show is decided by whether it prints a
per-user number.** Home and Jobs do — a "for you" count over a company you do not
watch counts jobs that can never reach your inbox — so they follow the person. The
picker, `GET /api/companies` and the Add flow do not, so they still see the shared
catalog. Nothing is hidden by the narrowing; it moved to the screen whose question
it answers.

**Each row on Home and Jobs is read-only except its category tag.** Until Phase 3
Slice 1a it also carried a switch writing `companies.tracked`, which is a
catalog-wide crawl flag and not a per-account setting — so on a screen that shows
everybody the same shared list, one user could stop the crawl for all of them.
Removing it left the pill as a label, and the note there said the control that
*should* live on a company row is a watch-list picker writing `user_companies`.
That is what `/watchlist` now is; `companies.tracked` stays sealed, and the two
have never been the same axis.

Both screens ship only counts, never job rows: sending ~790 rows of JSON with
the page to render twelve numbers would be wasted, and eleven of twelve rows
stay collapsed. A row fetches its own jobs from
`/api/companies/:id/jobs?filter=matches|all` the first time it opens, once. Each
job's title *is* its apply link (`rel="noopener noreferrer"`), because a
separate "apply" affordance would be decoration between a person and the
posting. Each row also carries the ATS, city, an `india-scoped` tag where
applicable, a `PM` tag, and a description hint (`desc not checked`, or the stated
minimum years) so an unenriched row says so rather than looking judged.

**Closed jobs are hidden everywhere** — both queries in
`dashboard/src/lib/jobs.ts` require `status = 'open'`, so a job closed by
Guard 3 (`miss_count >= 3`) disappears from both screens.

### `src/lib/` — what is where, and why some of it is split

| Module | Holds |
|---|---|
| `db.ts` | the postgres.js pool, `prepare: false` |
| `env.ts` | every env read, with `required()` for the ones that must not default |
| `jobs.ts` | the two job queries and `setApplied` — the app's only write to `jobs` |
| `companies.ts` | shared company queries + `upsertCompany`; read by routes *and* by server components directly |
| `preferences.ts` | load/save this user's row, `cleanList` |
| `watchlist.ts` | the per-user watch list: `watchCompany` (pair + history suppression, one transaction, per-plan cap), `unwatchCompany`, `watchesAnything`. The port of `db/watchlist.py` + `db/notifications.SUPPRESS_COMPANY_HISTORY_SQL` |
| `currentUser.ts` | the MY_EMAIL → `public.users.id` bridge, memoised. **Temporary; deleted at Phase 2 auth** |
| `labels.ts` | `Preferences`, `CITY_LABELS`, `KEYWORD_CAP`, `DEFAULT_PREFERENCES` (the install's filter, mirrored from Python), `NEW_ACCOUNT_SEED` (what a signup actually gets) and `ONBOARDING_PATH` (re-exported by `guard.ts`; the nav needs it and cannot import that file) — **constants with no database import** |
| `match.ts` | the port of `notify/match.py` |
| `ats.ts` / `workday.ts` | the port of `fetchers/resolve.py` |
| `board-url.ts` | a pasted URL → `(ats, token, config)`. Dashboard-only; no Python counterpart |
| `badges.ts` | the derived NEW / NOTIFIED / SUGGESTED rule |
| `guard.ts` | `requireSession()`, called at the top of every API route |

Two of those splits are load-bearing rather than tidiness.

`labels.ts` exists because a client component that imports a **value** from a
module drags that module's whole import graph into the browser bundle — and
`preferences.ts` imports postgres.js, which needs Node's `net` and fails the
build. Types are erased at compile time and travel anywhere; values do not. So
the constants live in a file that imports nothing.

`guard.ts` is belt-and-braces with `middleware.ts` on purpose. Middleware
protection is a regex matcher, and a matcher is one careless edit away from a
hole nothing tests. Re-checking inside the handler makes the guarantee local to
the code that touches the database: a route can be read in isolation and seen to
be protected. `CITY_LABELS` does similar work at the other end — the preferences
API rejects a city outside that set, because a typo there matches nothing,
silently, forever, and would otherwise be discovered three empty digests later.

### Status badges — derived, never stored

One badge per job row, computed at render from data the row already carries:

| Badge | Screen | Rule |
|---|---|---|
| `NEW` | Home | is a match **and** *I* have not been told — no `user_job_notifications` row for me and this job |
| `NOTIFIED` | Home | is a match **and** *I* have been told — such a row exists |
| `SUGGESTED` | Jobs | passes the same shared filter, on a list that does not apply it |

A non-matching row on Jobs gets no badge, which is the point — that screen is
several hundred postings including the sales roles, and SUGGESTED is what makes
the few that fit findable in it. Showing NEW/NOTIFIED there, or SUGGESTED on
Home, would put the same badge on every row and therefore say nothing.

**"Told me", not "told somebody" — the Phase 1f change.** The first two badges
read `jobs.notified` until 1f, and that boolean means "somebody was emailed about
this": a faithful paraphrase of "I was emailed about this" for exactly as long as
there is one account. With two, the first person's digest turns the second
person's NEW into NOTIFIED — a badge asserting an email that person never
received. The screen was the boolean's last reader, which is why 1f had to happen
before 1g could drop it. `user_notified` now comes from
`lib/jobs.notifiedToUser`, the **same predicate the digest queue asks** in
`CANDIDATES_SQL`, read from the other side: the queue asks `NOT EXISTS` to decide
what to send, the badge asks `EXISTS` to decide what to say. Two readings of one
table, and no third place for the rule to be restated slightly differently.

For the one real account both readings cover the identical set of jobs, so 1f
changed no badge on any screen — and that is verified rather than assumed:
`npm run badge-check` compares them against the live database and proves the
per-user half with a second account inside a transaction that always rolls back.

There is **no column** for any of this and there must not be. A badge is a
reading of the match rules plus a per-user fact, and the match rules read the
`preferences` row, which is editable from another tab — so a stored badge would
be a cached answer to a question whose inputs change under it, wrong within
seconds and wrong silently. Deriving it costs one boolean per row and cannot go
stale. `dashboard/src/lib/badges.ts` states the rule; the `matches` half comes
from `lib/match.ts`, the same parity-checked port the "for you" count uses.

That is also the argument behind the three-step type ladder in `lib/jobs.ts`,
which 1f is what made necessary: `JobRow` is the columns of `jobs` (the same
answer for everybody, forever), `UserJobRow` adds *have I been told about this*,
`JobView` adds *does it pass my filter right now*. Only the first is stored.
Keeping them as separate types is what stops the second and third drifting back
into the table, which is the mistake `jobs.notified` was.

NEW is not speculative. It was originally verified by temporarily flipping one
live match, confirming the badge, and restoring the row — because at that point
the table had been silently seeded and nothing else had ever been queued. Since
Step 6 it is simply the ordinary state: the hourly crawl queues genuinely-new
postings, they read NEW on Home until that hour's digest sends, and the receipt
that digest writes flips them. A job that **somebody else's** digest sent stays
NEW here, which is the whole point of the change.

### The Applied checkbox

Every job row on Home carries one. Ticking it `PATCH`es `/api/jobs/applied` with
the job's `canonical_id`; the route sets `applied` and stamps `applied_at`, and
unticking clears both. The row then dims, strikes its title through and sinks to
the bottom of its company — the apply link still works, because having applied
is the most likely reason to want to open the posting again.

Three details that are decisions rather than defaults:

- **The id travels in the body, not the path.** A `canonical_id` is built from a
  vendor's own identifier, and an id containing an encoded slash is a router 404
  before any handler runs — which would look like a missing job rather than a
  routing quirk. `/api/companies/:id` can use a path segment because that id is
  an integer.
- **`applied_at` is `COALESCE`d, not overwritten**, so it records when the box
  was *first* ticked and a repeated PATCH is genuinely idempotent. Unticking sets
  it to NULL, so the pair can never say "not applied, on the 3rd of August".
- **The sink-to-the-bottom ordering lives in the browser, not in the `ORDER BY`.**
  Ticking has to move the row immediately and the list is not refetched when it
  does, so the comparator has to exist client-side anyway; a second ordering rule
  in SQL would be a definition the client overrides a moment later.

**Auth**: Google OAuth via Auth.js v5, locked to exactly one account. Google's
"sign in" authenticates *any* Google account, so authentication is not
authorisation and the gap between them is the whole security model. The lock is
enforced three times — in `signIn` (refuses to mint a session), in the `jwt`
callback (so a session issued before `ALLOWED_EMAIL` changed does not outlive
the change), and in `isAuthorised` on every request. `src/middleware.ts` matches
everything except the sign-in page, Auth.js's own endpoints and static assets,
so a page added later is protected by default; each API route additionally calls
`requireSession()`. Every env read in `auth.ts` is softened to a fallback
because the module is evaluated at import time including during `next build` —
and **every fallback is the closed one**: a missing `ALLOWED_EMAIL` authorises
nobody.

**Auth is still `ALLOWED_EMAIL`, and that is a separate axis from Phase 1.** One
Google account may sign in; `MY_EMAIL` decides which `public.users` row that
session reads. The two must name the same person and nothing checks that they do
— which is safe only because the install has one of each, and is exactly what
Phase 2 replaces with a session carrying a real user id.

**Writes the dashboard is allowed to make**, and nothing else:
`companies.category`, this user's `preferences` row, inserting a company,
`jobs.applied` / `jobs.applied_at`, and — since the watch-list slice — **this
user's `user_companies` rows, together with the `user_job_notifications` rows
that suppress a newly-watched company's history**. Everything else
about a job stays read-only — the crawler owns the lifecycle, and a dashboard
able to edit `seeded` or `status` could silently un-send or re-send a digest.

The two per-user tables arrived on that list together and could not have arrived
separately: creating a tracking row without the suppression is the archive-dump
failure, so a picker that could write only the first would be worse than no
picker. They are written from `lib/watchlist.ts` and nowhere else —
`tests/test_watchlist_write.py` sweeps `dashboard/src` for any other `INSERT`,
`UPDATE` or `DELETE` against either table — and the notification rows it may write
are exactly the suppression ones. **The dashboard still cannot mark a job as
sent**: `record_sent` is the digest's, and nothing in the web app writes a
`notifications` receipt.
The applied pair is not a hole in that rule but the other side of it: those two
columns record something no crawl can observe, so nothing but the dashboard
could write them. `lib/jobs.setApplied` is the one statement in the app that
writes to `jobs`, and it names those two columns and no others; the route that
*lists* jobs is deliberately a different file, so "the listing route cannot
change a job" stays true by inspection. The dashboard still has no cron, sends
no email, and never fetches a job description.

**`companies.tracked` came off that list in Phase 3 Slice 1a, and it is the one
entry that was removed as a security fix rather than never added.** It was
writable from the start, through a toggle on every company row of Home and Jobs,
and it was defensible for exactly as long as `ALLOWED_EMAIL` meant the only
person who could reach the toggle was the person who owned the crawler. Phase 2a
opened sign-up and the control did not change: `requireSession()` asks whether
there is a session and never whose, the `UPDATE` was scoped by company id alone,
and `scripts/crawl.py` selects `FROM companies WHERE tracked = TRUE`. So any
account minutes old could switch the crawl off for a company — or for all fifty —
and every other account's digest would go quiet, with nothing recording who or
why. A dashboard write that can silence somebody else's inbox is the failure the
`applied` / `seeded` ownership split exists to prevent, one table over.

**Removed rather than gated**, which is the load-bearing half. Gating it to an
operator needs an operator identity, and there is deliberately none in the
deployed app — `lib/env.ts` has carried no `myEmail()` since 2b and says why: an
ambient identity read in `src/` becomes the thing the next page in a hurry
reaches for. Defending the flag that way would mean reversing 2b for a column the
web app has no business owning. A statement that does not exist cannot be reached
by curl or by a condition someone later edits the wrong way round, so
`updateCompany` dropped the column and `PATCH /api/companies/:id` refuses the
field outright with a 400 — 400 and not 403 because 403 implies somebody may, and
nobody may. The row still *shows* a tracked/off pill; reading a crawler fact is
not owning it.

What that costs, stated rather than glossed: **no script in this project sets
`tracked = FALSE`.** Every Python writer sets TRUE (`seed_companies.py`,
`add_workday.py`, `discover.TRACK_SQL`, the last being `ON CONFLICT DO NOTHING`
precisely so a sweep cannot undo a hand-set FALSE). The toggle was therefore the
repo's only off-switch, and switching a company off is now a statement against
the pooler by whoever holds the DSN — a rare operator action moving to the
runtime that already owns the column. `tests/test_tracked_write_sealed.py` is the
guard: no statement under `dashboard/src` may write it, with one enumerated
exemption for the Add upsert that a second test re-earns rather than assumes.

---

## Parity

Because there is no Python in the Vercel deployment, three modules are ported to
TypeScript: `src/lib/ats.ts` and `src/lib/workday.ts` from
`fetchers/resolve.py`, and `src/lib/match.ts` from `notify/match.py`. The last
one matters most — it is what the "for you" count *means*. A job counted there
must be a job the digest email would contain.

That is real duplication and it will drift, so two checks fail on any
disagreement:

| Command | Checks |
|---|---|
| `npm run parity` | `lib/ats.ts` + `lib/workday.ts` against `fetchers/resolve.py`, over fixed inputs |
| `npm run match-parity` | `lib/match.ts` against `notify/match.py`, over every open row in the live database, under **seven** filters |

Run `match-parity` after touching either matcher; its total is directly
comparable to `python scripts/preview_digest.py --dry-run --ignore-notified`.

**Seven filters rather than one, since Slice 2 and cities Slice B, and the reason
generalises.** The
live `preferences` row is `(0, 2)` with the no-experience toggle on — which is the
one branch of the new experience rule that reproduces the *old* rule. Running only
it would have proven the port did not regress and proven nothing whatever about
the three branches the slice added: a parity check that cannot reach the new code
is a green light bolted over an untested rule. So the same live rows are judged
under the real row plus `(2,5)`+off, `(0,0)`+on, and an unconfigured range,
differing **only** in the experience fields so any disagreement is attributable to
the rule under test. The last one is additionally asserted to be **empty** rather
than merely equal across languages — both sides agreeing on a wrong answer would
pass a comparison, and "an unset range matches nothing" is the property.

Cities Slice B added three more on the same argument, one field over: the live row
is a three-city selection, which is one of the city gate's three branches, so
running only it would say nothing about all-India or about the fail-closed rule.
The **no city answer** variant is asserted EMPTY for the same reason the
unconfigured range is — both languages agreeing on a wrong answer would pass a
comparison, and "a blank city answer matches nothing" is the property.

Current: **7,869 live rows × 7 filters, no disagreement**; the live row's total is
38, unmoved by either slice, and the variants report 45 / 18 / 0 / 343 / 0 / 0.

**The all-India variant's 343 is a measurement rather than a target**, and it is
the number that says the Slice A relabel has not reached this database. All-India
is "any label except `non-india`", and no live row carries `non-india` yet — the
deployed crawler is still the pre-Slice-A one, so the board is still labelled
`other` / `blr` / `hyd` / `pune` / `india-unclear`. So the city gate currently
admits every open row for an all-India account, foreign postings included, and the
keyword and experience filters are all that narrow 7,869 to 343. See *Pending*.

`match-parity` also prints how many rows carry a non-NULL `work_mode`, and since
the work-mode slice that line is a **failing gate** rather than a note: zero means
the backfill was never applied and the whole matrix is green over a rule live data
cannot reach. It is 102 (onsite 97, hybrid 5). No live row is `remote`, so the
relaxation is covered by a **synthetic probe** instead — four fabricated rows run
through both implementations and inserted nowhere.

Two more checks answer questions parity cannot. Neither is about drift between
the two languages; both are about a thing being *true against the real database*.

| Command | Asks |
|---|---|
| `npm run db:check` | does the transaction pooler actually work? Several parameterised and concurrent queries in a row, not one `SELECT 1` — the prepared-statement failure is never the first query, it is the second or third |
| `npm run config-roundtrip` | does a company added through the dashboard store `config` as a jsonb **object**? |
| `npm run badge-check` | is the NEW / NOTIFIED badge genuinely **per-user**? |
| `npm run watchlist-check` | does adding a company do **both halves** — the pair *and* the history suppression — and does the cap refuse the sixth without writing anything? |

`badge-check` is the Phase-1f/1g check and is shaped by what it cannot prove from
one account. §1 asserts the drop actually happened — `jobs` has no `notified` and
nothing in `dashboard/` names it — and asks both halves, because they fail apart:
a database the migration never reached still carries the column while no code
reads it, and a revert of 1g's diff has code naming a column the schema no longer
has. §2 is the interesting one. "A job notified to B reads NEW for me" cannot be
demonstrated by a user who owns no rows, so it **creates a second account inside
a transaction**, asks the production predicate under both identities, and rolls
back in a `finally` that does not depend on the assertions passing — then
re-counts outside the transaction to verify the rollback rather than assuming it.
§3 checks the "for you" counts come from my preferences row and nobody else's. It
imports `lib/jobs.notifiedToUser` and `lib/badges.homeBadge` themselves rather
than restating them, which is the same discipline `match-parity` enforces one
language over: a check that restates the rule proves a copy of it.

`watchlist-check` is shaped by the same problem `badge-check` has — the thing
under test cannot be seen from the account that already exists. The operator
watches all 50 companies and has for two phases, so "adding a company suppresses
its history" is unobservable there; the check creates a throwaway account through
`linkUser`, adds the company with the most stored rows, and then asks the digest's
own four clauses of it. The number it prints is the point: **1,180 rows recorded
as already-seen, and a queue of 0 against the 212 open non-seeded rows that would
otherwise have been that account's first email**. It then re-adds (0 suppressed),
fills the watch list to the cap, and asserts the sixth add is refused *and* wrote
nothing — the refusal is a throw inside the transaction, so the pair it had
already inserted rolls back with it. Real rows, deleted in a `finally`, for the
reason `onboarding-check` documents; the operator's counts are snapshotted before
and after.

`config-roundtrip` is the Step 5e bug turned into a test, and its assertion is
deliberately not "what I put in comes back out" — that would have **passed**
against the bug. `upsertCompany` bound `${JSON.stringify(config)}::jsonb`, and
postgres.js serialises jsonb with its own `JSON.stringify`, so the value was
encoded twice and the column held a jsonb *string* of JSON text. Every dashboard
screen looked right, because postgres.js parsed it back once on the way out. The
failure surfaced 30 minutes later, in the other language: asyncpg returns jsonb
as text, `Company.from_row` decoded it once into a `str`, and `config.get("host")`
raised `'str' object has no attribute 'get'` — which Guard 1 logged as a fetch
error, so the company crawled to nothing indefinitely while looking like an
unreachable board. So the check asks `jsonb_typeof(config) = 'object'`, of
Postgres, which is the only question whose answer both sides share. The empty
config is checked too and is not redundant: `'"{}"'::jsonb` is not `'{}'::jsonb`,
so a double-encoded blank also defeats the `ON CONFLICT` rule that refuses to
overwrite a stored config with an empty one — a re-add by name could then blank a
Workday row's host/site.

A further check answers a different question. `npm run ui-check` needs a **running**
dev server: it fetches real rows from the real API over HTTP with a minted
session (minted by `npm run mint-session`, which encrypts a token with the same
`AUTH_SECRET` and the same `encode` the real callback uses — Google's consent
screen needs a browser, which would otherwise make the entire authorised half of
the app untestable from a terminal), renders the shipped `JobLine` component with
them, and asserts the checkbox and the badges are actually in the markup. It exists because the Applied
checkbox and the badges were once specified, agreed and never built, and nothing
in the repo could tell the difference — a type checker proves the props line up
and a query proves the column exists, but neither proves a person opening the
page sees anything. What it does not cover is CSS and clicking, where a browser
would be needed and where the failure is visible rather than silent.

The city label is **not** ported. It is computed once in Python at crawl time
and stored in `jobs.city`; both sides read the column and neither recomputes it.
That is the same read-never-recompute contract `is_pm_role` and `min_experience`
follow — the evidence (a location string, a description) is not the stored thing,
the conclusion is.

---

## Env vars

Nothing here is ever committed. Values live in `.env` (crawler, gitignored) and
`dashboard/.env.local` (dashboard, gitignored), and are set again as GitHub
Secrets and Vercel Environment Variables at deploy time. `.env.example` and
`dashboard/.env.local.example` are the committed templates and hold only
placeholders.

| Var | Used by | Notes |
|---|---|---|
| `CRAWLER_DB_URL` | `db/connection.py` | Supabase **session** pooler, port 5432 |
| `DASHBOARD_DB_URL` | `dashboard/src/lib/env.ts` | Supabase **transaction** pooler, port 6543. Also present in root `.env` |
| `SUPABASE_CA_CERT` | `db/connection.py` | *optional* — path to the downloaded CA to verify the pooler certificate for real |
| `RESEND_API_KEY` | `notify/digest.py` | rejected if still the `re_xxxx` placeholder |
| `FROM_EMAIL` | `notify/digest.py` | `onboarding@resend.dev` (Resend's sandbox sender) unless a domain is verified |
| `MY_EMAIL` | `notify/digest.py`, `fetchers/http.py`, `dashboard/src/lib/ats.ts`, **`db/users.py`**, **`dashboard/src/lib/currentUser.ts`** | the contact address in the outbound User-Agent; the sandbox-sender assertion in `assert_self_send`; and, since Phase 1b, **the identity the current user is resolved from in both runtimes**. Falls back to `unknown@example.com` in the two User-Agent paths only. The digest requires it, and so does anything that loads preferences — including **the dashboard**, where it was previously optional. **Required on Vercel** |
| `GOOGLE_CLIENT_ID` | `dashboard/src/auth.ts` | |
| `GOOGLE_CLIENT_SECRET` | `dashboard/src/auth.ts` | |
| `AUTH_SECRET` | Auth.js | signs the session cookie; changing it logs you out everywhere |
| `ALLOWED_EMAIL` | `dashboard/src/lib/env.ts` | the one account allowed to sign in. **No default** — `required()` throws when unset, and every fallback path authorises nobody |
| `AUTH_URL` | Auth.js | *optional* — only if Auth.js cannot infer its own URL. Local dev and Vercel both infer it correctly |
| `PERSONAL_DENYLIST` | `scripts/githooks/pre-commit` | *optional* — extra values the personal-data guard should block |
| `GITHUB_TOKEN` | `fetchers/discovery.py` | *optional* — unlocks GitHub **code** search, which is 401 unauthenticated. Without it the sweep falls back to repository search + README mining and works fine, just narrower |

Because the sandbox sender can only deliver to the address that owns the API
key, `notify/digest.assert_self_send` refuses locally the send Resend would
refuse remotely — so a wrong recipient fails with a message naming the actual
constraint rather than as a 403 that reads like an auth error, and fails before
any network call and before any write.

**That guard was narrowed in 1e, and the narrowing is a tightening rather than a
loosening.** It used to be stated as an absolute — "the digest may only go to
`MY_EMAIL`" — which was an accurate paraphrase while there was one digest and one
person, but the *reason* it is true has always been the sandbox sender. With the
rule stated absolutely, every second user's digest would be refused forever and
verifying a domain would change nothing. So the guard now fires exactly when the
constraint exists: recipient equals `MY_EMAIL`, fine; `FROM_EMAIL` is not the
sandbox sender, fine (Resend will deliver, and refusing would be this function
inventing a restriction rather than reporting one); otherwise it raises. Today's
behaviour is unchanged to the character, because `FROM_EMAIL` *is* still
`onboarding@resend.dev` — **only the `MY_EMAIL` account can actually receive a
digest on this install, and a second real user's send fails loudly.** What
changed is that verifying a domain is now sufficient to make the fan-out real.

**`MY_EMAIL` as identity is a bridge with an expiry date.** `db/users.py` and
`dashboard/src/lib/currentUser.ts` resolve the current user by looking that
address up in `public.users`, in both runtimes, because auth is Phase 2 and there
is no session to ask yet. Deriving it from the address the install already had —
rather than adding a `DEFAULT_USER_ID` — is deliberate: two variables that must
agree about who you are can disagree, silently, and the failure surfaces as an
empty digest rather than as an error. **Both files are marked for deletion when
magic-link auth lands**, and neither is on the send path: the fan-out gets its
recipients from `public.users` and threads an explicit id down.

---

## Automation — the hourly cron

`.github/workflows/crawl.yml` is the whole of it. It orchestrates the two
scripts that were already proven by hand and adds no logic of its own:

```
every hour (cron '0 * * * *', UTC)   or   Actions -> crawl -> Run workflow
  └─ checkout -> setup-python 3.12 -> pip install -r requirements.txt
     └─ verify secrets -> python scripts/crawl.py -> python scripts/send_digest.py --tag cron
                          ─────────────────────     ─────────────────────────────
                          crawl once, user-blind    fan out, one email per user
```

The repo is public, so Actions minutes are free and unlimited.

**Two steps, and the asymmetry between them is the design.** `crawl.py` fetches
every tracked board once, for everybody, and knows nothing about users — it is
not per-user and never will be. `send_digest.py` is where all the narrowing
happens: for each active account, their preferences row, their tracked companies,
their queue, their email, their receipts. The step names say so
(*Crawl every tracked board*, then *Send the digests*), and the workflow gained
nothing else in Phase 1 — the fan-out is entirely inside the second script.

**One user's failure is not everyone's.** `run_fan_out` catches `DigestError`
per account — a refused recipient, a Resend outage — records it against that user
and continues, because the alternative is one broken account silencing every
other. Nothing is written for the user who failed: `run_digest` raises before any
row is touched, which is Guard 1 applied to email. The script then reports them
and exits non-zero, so the step goes red; a failure that appears only in a log is
a failure nobody sees. A user with zero matches sends nothing, writes nothing,
and never reaches `email_config()` — which is why the preflight secret check
below survives the fan-out unchanged and still does the work it always did.

**Why hourly for everything.** An earlier note here planned two tiers — 30
minutes for `tracked` companies, daily for the rest. There is no second tier to
run: `tracked = FALSE` means the company is excluded from crawls entirely, so
"daily for the others" would be a schedule for an empty set. One cadence for the
one set of companies is the whole requirement.

**`requirements.txt`, never `requirements-dev.txt`.** Nothing on the crawl or
digest path imports pytest — verified by grep over `crawler/`, `db/`,
`fetchers/`, `notify/` and the two entrypoints, and then by building a venv
containing only `requirements.txt` and running the full pipeline through it.
(`lxml` is pinned in `requirements.txt` but currently imported by nothing —
`fetchers/tier3.py` parses JSON-LD with `json` and `re`. It is harmless and
installs from a wheel; it is noted here so the next person does not go looking
for the import.)

### The three reliability properties

- **`concurrency.group` with `cancel-in-progress: false`.** Two runs must never
  overlap, because both write the same tables and a crawl reading a
  half-reconciled state would diff against a snapshot that was never really
  stored. Cancelling is the wrong way to enforce that: it would kill a run at an
  arbitrary statement and keep the *newer* one, when the older may already have
  jobs queued and an email in flight. Queueing means a slow run only delays the
  next one.
- **`timeout-minutes: 20`.** The ceiling was set against 14 boards; the watch
  list is **50** since Step 7 and the run still finishes inside it comfortably,
  because `fetchers/http.py` overlaps boards rather than walking them. The
  ceiling is for a vendor that accepts a connection and then never answers, and
  it sits well under the hourly cadence so a hung run cannot still hold the
  concurrency group when the next is due.
- **A bad board does not fail the job.** `crawl.py` exits non-zero only on a
  hard failure — an unreachable database, or a bug. One company's board being
  down is not that: Guard 1 in `crawler/loop.py` catches per company, records a
  tripped guard, writes nothing for it, and the loop continues. That shows as a
  `GUARD` cell in the step log on a green run, which is correct — one dead
  vendor is not a broken crawler. The corollary is worth knowing: a run in which
  *every* company tripped a guard is also green. The step log is what
  distinguishes them.

### Why the workflow checks the secrets before running anything

Zero matches is the normal hourly outcome, and `run_digest` reads
`RESEND_API_KEY` / `FROM_EMAIL` / `MY_EMAIL` only *after* it has decided
something matched. So a missing or misspelled email secret is invisible on every
quiet run and would first surface on the one hour a job actually matched — the
worst possible moment to discover it. The `Verify required secrets are present`
step asserts all four are non-empty up front, so a misconfiguration is a red run
on the very first execution instead. It tests presence only and never prints a
value.

### Secrets

Repository **Settings → Secrets and variables → Actions → New repository secret**.
Four, and only four — the values are the ones already in the local `.env`:

| Secret | Source | Note |
|---|---|---|
| `CRAWLER_DB_URL` | `.env` | Supabase **session** pooler, port **5432** |
| `RESEND_API_KEY` | `.env` | |
| `FROM_EMAIL` | `.env` | `onboarding@resend.dev` |
| `MY_EMAIL` | `.env` | must be the Resend account address, **and must match a `public.users` row** — the cron resolves the current user from it |

`DASHBOARD_DB_URL`, `GOOGLE_*`, `AUTH_SECRET` and `ALLOWED_EMAIL` are
dashboard-only and are deliberately **not** set here; the crawler never reads
them.

`CRAWLER_DB_URL` must be the **session pooler on 5432**, for the same reason
Step 1 chose it: the direct `db.<ref>.supabase.co` endpoint is IPv6-only and
GitHub runners are IPv4-only, so it would fail to connect from Actions even
though it works from a laptop. Copy it from `.env` rather than retyping it — the
password contains characters that must stay percent-encoded in a DSN, and an
unencoded one splits the URL at the wrong `@`.

### Running it by hand

Actions tab → **crawl** in the left sidebar → **Run workflow** → the green
button. `workflow_dispatch` exists for exactly this. It runs the identical
steps, so a manual run is a real crawl and a real digest, not a rehearsal.

### Reading a failed run

Actions → the red run → the `crawl` job → expand the step that failed.

| Step that failed | Almost certainly |
|---|---|
| `Verify required secrets` | a secret is unset or misspelled; the error names which |
| `Install runtime dependencies` | a dependency yanked or a wheel missing for 3.12 |
| `Crawl every tracked board` | the database is unreachable — check the DSN is the 5432 pooler, and that the Supabase project is not paused |
| `Send the digests` | Resend rejected a send, or one account failed; the message names the HTTP status and the affected address |

A red `Send the digests` **loses nothing**, and since the fan-out it loses
nothing *for the accounts that succeeded either* — the step is red if any user
failed, and the ones that sent are already recorded. A failed send is a no-write,
so those matched jobs stay in that user's queue (no `user_job_notifications` row
was written for them) and the next hourly run picks them up. The same is true of
a run that never started: the queue is a set of rows, not an in-memory list, so a
skipped hour is deferred rather than dropped. GitHub's scheduler is best-effort
and does run late under load — the cron is a floor on the interval, not a
promise.

## Build status / changelog

Newest last. Append here as the project evolves.

- **2026-07-26 — Step 1.** Project skeleton, Supabase schema, DB connection
  helper (session pooler, IPv4 rationale, `statement_cache_size=0`).
- **2026-07-26 — Step 2.** Tier-1 ATS fetchers (6 vendors) + Tier-3 JSON-LD and
  sitemap, the one canonical row shape, fixture archive.
- **2026-07-26 — Step 3.** Diffing engine, crawl loop, the three guards, silent
  seed, tests.
- **2026-07-26 — Step 4.** Resend digest + matching; then a resume-grounded
  keyword set (core + ML/AI); then seniority-level exclusion (`max_level`) with
  the experience-number mask; then the description-aware experience and PM
  filters.
- **2026-07-26 — Step 2b.** Workday fetcher — paginated POST, India-scoped via
  facet discovered *by value*, `india_scoped` flag, three independent paging
  stops. *(Last commit pushed before Step 5.)*
- **2026-07-26 — Step 5.** Dashboard: Next.js App Router, Google OAuth locked to
  one account, transaction pooler with `prepare: false`, add-by-name and
  add-by-URL, preferences editor, TS ports + parity checks, and the pre-commit
  secret guard.
- **2026-07-26 — Step 5b.** Home matches view, per-company collapsible job
  lists fetched on demand.
- **2026-07-26 — Step 5d.** Enrichment moved into the crawl, closing the hole
  where a freshly-discovered 3+-year role sat on Home looking like a match.
  Added `desc_fetch_failed` / `desc_checked_at` and the once-a-day retry.
- **2026-07-27 — Step 5e.** Fixed the dashboard writing `companies.config`
  double-encoded; `scripts/repair_config.py` to repair affected rows, and a
  tolerant decoder in the crawler so a repaired crawler works against an
  unrepaired database.
- **2026-07-27 — Step 5f.** City-code abbreviations (`blr`/`hyd`/`pnq`,
  country-prefixed `ind pun`) + re-label. Recovered 135 Bengaluru jobs.
- **2026-07-27 — Step 5g.** City misspelling `banglore` + re-label, chosen from
  a data survey rather than from guesses. 3 rows.
- **2026-07-27 — Step 5h.** Dashboard User-Agent contact address read from
  `MY_EMAIL` instead of a hardcoded literal.
- **2026-07-27 — Personal-data guard + this file.** Pre-commit hook extended
  with a personal-data check (real-contact-address detection, git-identity
  derivation, `PERSONAL_DENYLIST`); the unpushed dashboard history squashed so
  no blob in it carries a contact address.
- **2026-07-27 — Step 5c.** The Applied checkbox and the derived NEW / NOTIFIED /
  SUGGESTED badges — both specified in an earlier round, agreed to, and never
  actually built. The gap survived because nothing in the repo could tell a
  rendered feature from a designed one, so this step also added
  `npm run ui-check`: it renders the shipped row component over live rows from a
  running server and fails if the markup does not contain them. Schema gained
  `jobs.applied` / `jobs.applied_at`, the only two columns on that table the
  dashboard owns, with `tests/test_applied_column.py` proving the crawler never
  writes them.

- **2026-07-27 — Step 6.** The hourly GitHub Actions cron — the step that makes
  the tool self-running. `.github/workflows/crawl.yml` runs `crawl.py` then
  `send_digest.py --tag cron` every hour, behind a concurrency group, a 20
  minute timeout and a preflight secret check. The preflight exists because
  `run_digest` reads the email credentials only after something has matched, so
  a missing secret would have stayed invisible until the first real match.
  Verified before push by running the whole pipeline through a venv built from
  `requirements.txt` alone.

- **2026-07-27 — Step 7.** Discovery. `scripts/run_discovery.py` sweeps every
  candidate name it can get — a 212-name seed list, GitHub README mining, and
  any `--seed-file` — and auto-tracks the ones that clear two bars: resolves to
  a Tier-1 board, and has a real open job in Bangalore, Hyderabad or Pune.
  **`companies` went 14 → 50**; 311 candidates tried, 86 resolved to a live
  board, 36 auto-tracked, 50 resolved with nothing in the three cities (logged,
  not tracked), 199 unresolved of which 96 are flagged `try Workday`.
  `discovered_companies` holds all 310 as the audit trail.

  Two things it deliberately does not do, both asserted in the tests rather than
  promised: it never writes `jobs` (so the next crawl still silently seeds every
  new company), and it never `DO UPDATE`s a `companies` row (so a hand-edited
  name, a hand-tagged category, or a deliberate `tracked = FALSE` survives every
  future run). Both were then confirmed against the live database: the hourly
  cron seeded **6,496 rows** for the new companies while the sweep was still
  running, and not one of them was queued for email.

  Also here: the lockstep-batching mistake and its fix (see *Why the sweep
  probes 24 candidates at once*), which is the difference between a 2.5-hour
  sweep and a 45-minute one at identical politeness.

- **2026-07-28 — Step 7b. The Lucknow leak.** An InMobi "SDE 2" posted in
  Lucknow, asking for 3–6 years, reached the inbox as a match. Two independent
  filters should each have stopped it and neither did, and the diagnosis matters
  more than either fix because the first hypothesis was wrong: enrichment was
  **not** skipping the 36 discovery-added companies. All 100 open title-stage
  matches were enriched, InMobi's included — `ENRICHMENT_TARGETS_SQL` never
  filtered on company and `crawl.py` runs the pass after every board.

  What actually happened was two separate things.

  *City.* `city_label` put every India signal in one bucket, so "Lucknow"
  labelled `india-unclear` — the same label as "Remote" — and `india-unclear`
  was in `preferences.cities`. Named Indian places outside the three cities are
  `other` now, and the seeded city list is three cities exactly. A sweep found
  this was never one job: **14 of 61 matches** were `india-unclear` and every one
  of them named a place — Noida (Adobe, Cadence), Mohali (Zscaler), Lucknow
  (InMobi), plus "Remote, United Arab Emirates" and "Remote - SF Bay Area",
  which reached that label by naming no marker at all. `relabel_cities.py` moved
  **501 stored rows** `india-unclear -> other`, one transition and no other, with
  the lifecycle fingerprint identical before and after.

  *Experience.* The row was read, cleanly, and the parser found nothing in
  "3–6 years in data/platform engineering". Both existing anchors were
  individually right to miss it: `_RANGE_RE` found the range and
  `_line_has_context` threw it away because the bullet says "engineering" rather
  than "experience", and `_YEARS_RE` is forbidden from reaching the word on the
  next bullet. A requirements list that never uses the word "experience" is
  completely ordinary, and it stated no requirement at all. So position is now a
  third anchor: a duration that OPENS its own line is a requirement — with a
  lookahead excluding the shapes that can lead a line and still not be a career
  ("5 years of service award", "10 years ago"), because a perks list is bulleted
  exactly like a requirements list and reading one as a minimum deletes a job
  silently.

  Two things this step also produced. `normalize.india_funnel` — because `other`
  stopped meaning "not India", and the Workday unfiltered fallback was still
  asking it that question; left alone it would have quietly stopped *storing*
  Mumbai rows. And `scripts/recheck_descriptions.py`, because a parser change
  does not reach rows that already hold a conclusion, which is the same problem
  `relabel_cities.py` exists for and needed a differently-shaped answer.

- **2026-07-28 — Step 7c. The SmartRecruiters description route.** Finishing 7b
  turned up the third way the same failure could happen, and it was not a bug in
  a detector — it was a detector that never ran. `fetch_description` had no
  SmartRecruiters route, so 12 of the 46 surviving matches (10 Continental, 1
  Lendingkart, 1 Unacademy) came back `desc_fetch_failed`, and unknown means
  KEEP. Twelve jobs were in the digest on no evidence at all about their
  experience requirement — the exact state the InMobi row was in, on a quarter of
  the list.

  The public posting API answers it, unauthenticated:
  `api.smartrecruiters.com/v1/companies/{token}/postings/{id}` ->
  `jobAd.sections.{companyDescription, jobDescription, qualifications,
  additionalInformation}`. All four are read. `qualifications` is the
  load-bearing one and is Lever's trap on a different vendor: it is where every
  years-of-experience line lives, so reading only `jobDescription` would have
  reintroduced the bug one field lower down. `additionalInformation` is the perks
  bucket and is read anyway — the detectors are built to survive a perks list
  rather than to be kept away from one, which is precisely what the new
  leading-duration lookahead is for.

  It worked immediately and the numbers moved again: **31 descriptions read, 0
  failed** (was 12 failing), and **8 Continental roles turned out to want 3–6
  years**. So the final count is **61 -> 38**, not the 46 that 7b alone
  predicted — 46 was only ever "46 that survived the two filters we could
  actually run". `match-parity` agrees with `notify/match.py` on all 7,693 live
  rows, `preview_digest --dry-run --ignore-notified` reports the same 38, and of
  those 38: **0 in the wrong city, 0 unread, 0 unavailable** (blr 34, hyd 3,
  pune 1).

  One ordering note worth keeping, because the obvious order is wrong.
  `recheck_descriptions.py` selects on `title_stage_ok`, which reads `jobs.city`
  — so it must run *after* `relabel_cities.py`, never before. Run first, it would
  clear `desc_checked` on rows that were about to become wrong-city, and
  enrichment never asks about those, so the flag would sit at FALSE forever
  recording "nobody has read this" about a description that had been read. That
  is the hazard the script's own docstring names, reached from the other side.

  The 24-hour retry gate also had to be stepped around once, deliberately:
  `RETRY_AFTER` exists so a **down board** is not hammered every half hour, and
  these twelve rows were not a down board — they were a route that did not exist.
  Their last look was 14 hours old, so an ordinary crawl would have skipped them
  for another ten. `enrich_matches(now=...)` is the injection point; shifting
  `now` forward by exactly `RETRY_AFTER` makes the cutoff the real present, which
  re-asks every failed row once and cannot touch a clean read (those are never
  re-read whatever the cutoff says).

- **2026-08-02 — First week unattended, and a doc-drift pass.** No code changed;
  this entry records what running the thing for five days actually showed, and
  what reading the doc against the tree turned up.

  *The system runs.* The hourly cron has been crawling all **50** companies
  without intervention — most recent sighting 2026-08-02 05:04 UTC, and **every
  one of the 50 has stored rows**, so no board was tracked into permanent
  failure. **8,587** jobs stored, **7,674** open, **913** closed by Guard 3.
  Newest first-sighting 2026-08-01, so the diff is still finding genuinely new
  postings a week in rather than having gone quiet.

  *The inbox is quiet, which is the design working.* Five digests exist in
  `notifications`, fifteen receipts in total: `2026-07-26-first-real` (2),
  `2026-07-27-local-verify` (5), then the cron's own — 07-28 (5), 07-29 (2),
  07-31 (1). **07-30 and 08-01 produced no digest at all**, because nothing
  matched. That is the outcome the preflight secret check exists for, now
  observed in production rather than argued for: two of five days sent nothing,
  so a mistyped email secret would genuinely have gone unnoticed on those days.
  Note also that 7,706 rows carry `notified = TRUE` against fifteen receipts —
  the gap is the silent seed, exactly as intended.

  *7c held.* Of the open jobs, **zero** carry `desc_fetch_failed` — the
  SmartRecruiters route did not regress, and no other vendor quietly became a
  route that does not exist. Only 89 open rows are `desc_checked`, which is the
  title-stage seam doing its job: enrichment reads a few dozen descriptions, not
  7,674. The stored city spread is `other` 5,838 / `blr` 1,342 / `hyd` 198 /
  `india-unclear` 192 / `pune` 104 — *store loosely* made visible, and the
  `india-unclear` bucket is now small and vague-only rather than the 501-row
  mixed bag 7b relabelled.

  *Doc drift found and fixed here.* Four things the file described less
  accurately than the code deserved: discovery was attributed entirely to
  `scripts/run_discovery.py` when the two "never" guarantees are actually
  properties of `crawler/discover.py`'s SQL; `src/lib/` had no map, hiding two
  load-bearing splits (`labels.ts` importing no database so client components can
  import values, `guard.ts` re-checking what middleware already matched);
  `npm run db:check` and `npm run config-roundtrip` were undocumented despite
  `config-roundtrip` being the Step 5e bug turned into a test; and the timeout
  rationale still said "14 boards". Also noted for the first time: **four of the
  nine supported ATSes have no tracked company**, so `workable`, `recruitee` and
  both Tier-3 routes are fixture-tested only.

- **2026-08-02 — Phase 1a. The user stops being implicit.** `public.users` —
  uuid primary key, `email` unique, a unique index on `lower(email)`, `plan` and
  `is_active`. `scripts/seed_user.py` creates the row from `MY_EMAIL`, so a fresh
  database is reproducible without the repo ever holding an address.

  The whole point of the step is that it changes nothing. **Nothing reads the
  table**: no crawler path, no dashboard route, no foreign key. It is
  deliberately inert, so adding it could break nothing, and every sub-phase after
  this one is a key pointing back at it.

  Two decisions worth the ink. `UUID` rather than the `SERIAL` the rest of the
  schema uses, because Phase 2's magic-link auth keys users by uuid on both
  Supabase Auth and the Auth.js adapters, and a `SERIAL` would mean carrying a
  provider-id column to join on forever. And **every reference is schema-
  qualified `public.users`**, because Supabase ships its own `auth.users`: a bare
  `CREATE TABLE IF NOT EXISTS` resolves against a `search_path` this project does
  not set, so with `auth` ahead of `public` it would find that table, skip,
  report success, and leave `public.users` uncreated. A silent failure, not a
  loud one — which is why the qualification is a rule rather than a preference.

- **2026-08-02 — Phase 1b. The filter gets an owner.** `preferences.user_id`
  (`UUID` → `public.users`, `ON DELETE CASCADE`, nullable) plus
  `preferences_one_per_user_idx`, a *partial* unique index so named
  multi-profiles stay one `DROP INDEX` away. `WHERE id = 1` is gone from every
  loader in both runtimes; `scripts/backfill_preferences_user.py` attaches the
  existing row. Behaviour unchanged — match-parity clean, selection fingerprint
  identical. 8 new tests, 553 total.

  This is where the **MY_EMAIL bridge** appears, in both runtimes at once:
  `db/users.py` and `dashboard/src/lib/currentUser.ts`, each resolving
  `public.users WHERE lower(email) = lower(MY_EMAIL)`. Reusing the address the
  install already had rather than adding a `DEFAULT_USER_ID` is the decision —
  two variables that must agree about who you are can disagree silently, and the
  symptom is an empty digest rather than an error. Both files are marked for
  deletion at Phase 2.

  The half that did not change is the failure mode. `user_id` had to arrive
  nullable (`ADD COLUMN … NOT NULL` needs a default the schema cannot know), so
  there is a window in which the row exists unowned and a user-scoped loader
  finds nothing. Falling back to the seeded defaults there would silently replace
  a filter someone tuned and *email the results* — so both loaders spend one
  extra query on the miss path to tell "no row for this user" from "the migration
  is half-applied", and **raise** on the second. Applying the schema and running
  the backfill is one unit with no crawl or digest between them.

  `CHECK (id = 1)` was deliberately left standing. A PK reshape with no consumer
  is a speculative change; it comes down in 1e, which is the first sub-phase that
  genuinely needs a second row.

- **2026-08-03 — Phase 1c. The watch list becomes per-user.** `user_companies`
  joins `public.users` to `companies`, both sides `ON DELETE CASCADE`, unique per
  pair. `scripts/backfill_user_companies.py` tracks all **50** catalog companies
  for the single user resolved from MY_EMAIL, so behaviour is unchanged: the
  digest that reported 34 matches before the migration reports the same 34 after.

  The point of the step is the half that did **not** change. Crawling is not
  conditional on the watch list and must never become so — `COMPANIES_SQL` is
  byte-for-byte what it was, no crawl-path file was touched, and the invariant is
  a test rather than a note in this file. The reasoning is the silent seed: a
  company nobody happens to watch this week would go dark, and `crawl.py` decides
  seeding from the stored row count, so the day it was added back it would
  re-alert its whole back catalogue. "Crawl once, fan out" means the crawl stays
  user-blind and the *notification* narrows; the fan-out itself is 1e.

  Two smaller things worth keeping. The backfill's success gate asserts coverage
  of a catalog snapshot taken before the insert rather than re-counting the
  catalog afterwards — the discovery sweep and `add_workday.py` can both add a
  row mid-run, and the arithmetic gate would have failed a perfectly good
  migration for it. And `tests/conftest.py` gained an opt-in
  `enforce_foreign_keys()`, because SQLite ignores every `REFERENCES` clause
  unless `PRAGMA foreign_keys` is on: without it the two CASCADE tests pass
  vacuously by asserting nothing. Neutering the pragma fails all three FK tests,
  which is how that was confirmed rather than assumed.

- **2026-08-03 — Phase 1d. "Notified" gets an owner.**
  `user_job_notifications` — `user_id` `UUID`, `canonical_id` `TEXT` (because
  `jobs` has no `id` column and `notifications` already spells the key that way),
  both `ON DELETE CASCADE`, `UNIQUE (user_id, canonical_id)`, reverse index on
  the job side. A row existing *is* the decision; there is no flag on it and no
  state in between. The backfill recorded every `notified = TRUE` job against the
  MY_EMAIL user behind a two-part gate — anti-join uncovered `== 0` **and** count
  equality — refusing rather than passing on a mismatch. 20 new tests, 634 total;
  live: uncovered 0, digest a no-op, parity 34 unchanged.

  The half that did not change is `jobs.notified`, and deliberately: the new
  table became the **read** authority while the boolean went on being **written**
  in the same transaction. Three reasons, all of which later expired — it was the
  rollback net (reverting 1d could not re-send anything sent during the 1d
  window), it was the backfill's only accurate source for a re-run, and the
  dashboard badge still read it, so dual-writing kept the screen honest without
  touching a line of TypeScript.

  The queue this step could ship was therefore a **conjunction, not a swap**:
  `notified = FALSE AND NOT EXISTS (…)`. The first clause was load bearing on the
  *crawl* path — the silent seed wrote `notified = TRUE` across a whole back
  catalogue, and those rows have no per-user row at all, so reading the new table
  alone would have queued an entire freshly-seeded board. One discovery sweep had
  seeded 6,496 rows in an afternoon. That obligation was written into
  `schema.sql` and into `CANDIDATES_SQL` as a note addressed to 1e.

- **2026-08-03 — Phase 1e. The fan-out — crawl once, notify each.**
  `run_fan_out` loops `run_digest` over every active account: their preferences,
  their tracked companies, their queue, their filter, their own address, their
  receipts. Isolation is in the SQL (every statement bound to one `user_id`)
  rather than in the loop being careful, and one user's `DigestError` is recorded
  against them while everybody else still gets their digest. `scripts/
  preview_fanout.py` shows what every user *would* receive, with an overlap
  report — a job at a company two people watch should appear twice; a job at a
  company only one watches appearing twice is the leak the script exists to make
  visible. 25 new tests, 679 total; proven live with a real two-user run.

  It discharged 1d's obligation by **replacing the seed marker outright** rather
  than keeping the conjunction. `jobs.seeded` now carries "this row is back
  catalogue" — written by the same `mark_new` call from the same `seeding` flag,
  read by the queue in the exact position `notified = FALSE` occupied. So the
  queue became per-user in the two clauses that are about a person and user-blind
  in the one that describes a crawl.

  The hazard this step *found* is the one worth remembering, because
  `jobs.seeded` does not cover it. A user who starts tracking a company that has
  been in the catalog for weeks inherits hundreds of perfectly ordinary rows —
  not seeded, never emailed to anyone, sitting open — and they would all be that
  person's first digest. So `track_companies` writes the pair and records that
  company's stored history as already-notified **in one transaction**, only on a
  genuine insert. The bulk `TRACK_ALL_COMPANIES_SQL` from 1c was *deleted* rather
  than left unused: a convenient statement that skips the second obligation is a
  loaded gun aimed at whichever future caller reaches for it.

  Two smaller removals. `CHECK (id = 1)` came off `preferences` with the sequence
  restored, exactly as 1b predicted — a fan-out with one filter is not a fan-out,
  and it is the second *filter*, not the second user, that proves a digest was
  computed for a person. And `assert_self_send` was narrowed to fire only for the
  sandbox sender: stated as an absolute it would refuse every second user's
  digest forever and verifying a domain would change nothing. Live behaviour
  identical to the character.

  Untouched, and asserted so: the crawl path, byte-for-byte.

- **2026-08-03 — Phase 1f. The last reader moves.** `homeBadge` derives NEW /
  NOTIFIED from `lib/jobs.notifiedToUser(userId)` — the `EXISTS` mirror of the
  digest's `NOT EXISTS`, same table, same predicate — and `jobs.notified` came
  out of `jobColumns()` so it travels on no row at all. The type ladder
  `JobRow → UserJobRow → JobView` is what keeps the three kinds of fact apart:
  the columns, the one per-user fact, and the filter's verdict at render time.
  `setApplied` stays column-only, with `user_notified` re-attached client-side.
  45-case invariant added, 724 total; match-parity 34, typecheck and ui-check
  clean.

  The change is one of **meaning** — "somebody was told" to "I was told" — and
  for a single account the two cover the identical set of jobs, so no badge on
  any screen moved. That is exactly why it could not be proved by looking at a
  screen: `npm run badge-check` asserts a symmetric difference of zero for the
  real user, then proves the per-user half in both directions against a second
  account created inside a transaction that always rolls back. A textual
  invariant (`tests/test_dashboard_notified.py`) now forbids `.notified` across
  the whole dashboard, with `badge-check.ts` the single enumerated exemption,
  scheduled to expire with 1g.

  Deliberately untouched: the matchers, the crawler, the digest, and the
  dashboard's company list — which is still the shared catalog rather than your
  watch list, and stays that way until Phase 3.

- **2026-08-03 / 08-04 — Phase 1g. The column goes, and isolation stops being a
  thing a person checked once.** Two commits, in the only safe order.

  *Code first.* Nothing in the deployed runtime touches `jobs.notified` any more:
  `UPSERT_SEEN` no longer names it, `mark_new` writes only `seeded` (and its
  parameter is named for what it writes again), `record_sent` is two writes in
  one transaction, `ENRICHMENT_TARGETS_SQL` drops it from the SELECT list,
  `crawl.py` reports seeded/eligible, and `relabel_cities`' fingerprint guards
  the column that now decides. The DROP was **deliberately not in that commit**:
  the deployed cron and dashboard were still running code that read the boolean,
  and old code against a dropped column is an outage, not a no-op. Expand, then
  contract.

  *Then the schema.* `ALTER TABLE jobs DROP COLUMN IF EXISTS notified` — the
  first `DROP COLUMN` in the file, following the shape the existing
  `DROP CONSTRAINT IF EXISTS` already set: the `CREATE TABLE` keeps describing
  the table as it was built, and the removal is stated once, where there is room
  to say why. The 1d and 1e backfills were deleted alongside it, because both
  read the column and neither could outlive it.

  Three things had to be true first, and all three were: no page render, query or
  badge read it; the code that stopped reading it was **deployed**, not merely
  written; and every job the boolean called notified was already recorded
  per-user, with every silently-seeded row already carrying `jobs.seeded`.

  Why it survived three sub-phases after its last reader, and why it stopped:
  **a net for a rollback nobody can still perform is not a net.** Once 1e was
  proven in production the earlier phases were not revertible in any case, and a
  column written by one statement and read by none is worse than absent — the
  next person to find it reasonably assumes something depends on it, and the
  first edit made on that assumption is a silent one.

  `tests/test_leakage.py` is the other half of the commit and the more durable
  one. 1e and 1f were verified **by hand**; this states five properties that run
  on every commit — no job crosses between two digests (all four watch-list ×
  filter cells at once), a write for one user touches no row of another's (both
  directions, both slices snapshotted), send order does not matter, a shared job
  is one receipt *per person*, and tracking-time suppression is per-user. Every
  test that could be satisfied by a difference in *filters* gives both users the
  same filter, so only per-user state can separate them; each property was
  confirmed to fail under a deliberate mutation of the clause it defends. The
  suite ends Phase 1 at 724, with the column absent from `information_schema`.

- **2026-08-08 — Phase 3 Slice 1a. Sealing the global crawl switch.** A
  dashboard-only security fix, and a subtraction: `companies.tracked` is no
  longer writable from the web app. It is the crawler's catalog-wide exclusion
  flag — `scripts/crawl.py` selects `FROM companies WHERE tracked = TRUE`, one
  shared value for the whole install — and every company row on Home and Jobs
  carried a checkbox that PATCHed it. `requireSession()` asks whether there is a
  session and never whose, and the `UPDATE` was scoped by company id alone, so
  after 2a opened sign-up any account minutes old could switch the crawl off for
  a company, or for all fifty, and silence every other account's digest with
  nothing recording who or why.

  The write was **removed rather than gated**, and that is the decision worth
  keeping. Gating needs an operator identity; `lib/env.ts` has deliberately
  carried none since 2b, whose comment warns that an ambient identity read in
  `src/` becomes what the next page in a hurry reaches for. So a gate would have
  meant reversing 2b to defend a column the web app has no business owning.
  `updateCompany` dropped it and is category-only; the route refuses the field
  with a **400 rather than a 403**, because 403 implies somebody may and nobody
  does — the statement is gone for every caller. The pill stays as a label:
  reading a crawler fact is not owning it.

  What it costs is real and small: **no script in this repo sets
  `tracked = FALSE`** (every Python writer sets TRUE, and `discover.TRACK_SQL` is
  `ON CONFLICT DO NOTHING` precisely so a sweep cannot undo a hand-set FALSE), so
  that toggle was the only off-switch anywhere. Switching a company off is now a
  statement against the pooler by whoever holds the DSN.

  Two guards, because they fail apart, which is the same reason
  `npm run badge-check` asks both its questions. `tests/test_tracked_write_sealed.py`
  asserts no statement under `dashboard/src` writes the column — one enumerated
  exemption for the Add upsert, re-earned by its own test rather than granted —
  and it was confirmed to fail against the pre-fix tree rather than assumed to
  work. `npm run ui-check` renders the shipped `CompanyList` over a live card and
  asserts the markup carries the pill and no `Track <company>` control, because a
  sealed route with the button still on screen is a bug report waiting to happen.

  Deliberately **not** in this slice: the per-user watch list. `user_companies`
  is the control that belongs on that row and it is the next slice; nothing here
  is a stand-in for it. Also untouched: the Home match query, the `companies.ts`
  read path, matching, the crawler, and every line of Python outside the new test.

  *(Note for whoever writes the history: this changelog jumps from 1g to here.
  Phases 2a–2c, 3 Step 0 and 3 Slice 1 shipped without entries — the commits are
  in the log and the prose above describes the code as it stands, but the
  narrative for those five is missing.)*

- **2026-08-08 — Phase 3 Slice 1b. A new account stops being handed the
  operator's filter, and stops being able to skip setup.** Two faults that only
  looked like one, because each made the other invisible.

  *Nothing routed anybody to setup.* `onboarding_completed` had been written
  since Slice 1 and read by nothing, so a Google account seconds old landed on
  Home — the whole 50-company catalog, with match counts — and was never shown
  the screen that asks what it wants. `lib/guard.requirePage()` now redirects on
  that column, with the three enumerated exemptions above. It is inside
  `requirePage` rather than beside it so that a page added later is gated by
  default, and `/preferences` is exempt because **deleting an account lives
  there**: a person who signed up and changed their mind must not have erasure
  held behind a setup screen.

  *And the setup screen could not have been completed anyway.* Branch 3 of
  `lib/link-user.ts` seeded every new row from `DEFAULT_PREFERENCES` — 43
  keywords, over the `KEYWORD_CAP` of 25, so `PUT /api/preferences` refused the
  save; and that save is the **only writer** of `onboarding_completed`. The seed
  was refusing the one act that finishes onboarding. Each of the three parts was
  individually right, which is why nothing in the repo could see it: a
  typechecker proves the constant is a string array, a test proves the cap is
  enforced, and neither carries an account from signup through to a completed
  save. `NEW_ACCOUNT_SEED = {...DEFAULT_PREFERENCES, keywords: []}` is the fix —
  a spread rather than a hand-copied literal, because `DEFAULT_PREFERENCES`
  keeps its own meaning as the Python-mirrored parity anchor and re-typing three
  of its four fields is how the copy nothing compares drifts.

  Only `keywords` is emptied, and the other two are a decision rather than an
  omission. `cities` is refused empty by the same route and three cities are the
  entire scope of the product today; `exclusions` is a *narrowing* list, so
  blanking it would widen a new person's first digest to the manager, sales and
  recruiter titles `max_level` cannot catch. Both are on the screen the moment
  somebody disagrees. `is_active` stays FALSE, unchanged.

  `npm run onboarding-check` is the proof and it is shaped by what the other
  checks cannot do. It calls `linkUser` itself with a synthetic Google user —
  not a forged session; a plain object passed to a function, where `ui-check`
  explains at length why it will not mint a cookie — then reads the row branch 3
  wrote, asks the gate's own predicate, saves one keyword through
  `savePreferences`, and asks the predicate again. Real rows deleted in a
  `finally` rather than a rolled-back transaction, because `savePreferences`
  binds the pool and could not see an uncommitted account; the operator's row is
  snapshotted before and after, `updated_at` included. 24 assertions, all green
  against the live database.

  The textual guards were **mutation-checked, and the first draft of one failed
  that check**: it looked for `onboardingCompleted` anywhere in `guard.ts`, and
  deleting the entire redirect left the import behind, so the gate could be
  removed wholesale with the test still green. It now names the call, the
  redirect and the condition separately. Six deliberate breakages, six caught.

  Deliberately **not** in this slice: the per-user watch list, the Home match
  query, the matchers, the crawler, and the app shell — so the nav still shows
  Home / Jobs / Add during onboarding and those tabs bounce back, which is the
  gate being visible rather than a bug, and is listed under *Pending*.

  `scripts/reset_unonboarded_keywords.py` is the manual half, in its own commit
  after being run — the same expand-then-contract ordering 1g used. A seeding fix
  reaches only accounts created after it, and the one account already sitting
  mid-onboarding was seeded with the 43. Its guard is two clauses
  (`onboarding_completed = FALSE` **and** `keywords` exactly equal to the seeded
  list, in order) because either alone is wrong in a way nobody would notice: the
  first would delete a list somebody had typed and not yet saved, the second
  would clear the filter of an account that kept the defaults on purpose.

- **2026-08-08 — Phase 3 Slice 1c. The rest of the operator's filter comes off
  the seed.** Slice 1b emptied `NEW_ACCOUNT_SEED.keywords` and deliberately left
  `cities`, `exclusions` and `max_level` as this install's defaults. This finishes
  that argument, because the argument was never about keywords in particular.

  Slice 1b had two reasons and only the second was specific to that list: the 43
  were somebody else's answer, **and** 43 is over `KEYWORD_CAP`, which made the
  first save impossible. Reason 2 is what made keywords urgent; reason 1 is true
  of every field on the row. A seeded `cities` of blr/pune/hyd is the operator's
  answer to "where do you want to work"; a seeded `exclusions` of fourteen terms
  silently drops every architect and analyst posting for somebody who may be
  looking for exactly those; `max_level = 2` answers "how senior". None were
  chosen by the person holding the account, and on the screen a default nobody
  chose is indistinguishable from a decision they made.

  `NEW_ACCOUNT_SEED` is now three empty lists and nothing else. **The spread came
  out**, reversing 1b's decision on purpose: it existed to stop three copied
  values drifting from the Python, and empty cannot drift — while a spread would
  now be actively dangerous in the one direction that matters, since any field
  added to `DEFAULT_PREFERENCES` later would arrive on new accounts as the
  operator's answer, silently. `DEFAULT_PREFERENCES` itself is **untouched** and
  keeps its meaning as the parity mirror of `notify/match.py`'s `DEFAULT_*`.

  **Two of the blanks are absences rather than empty values, and both are forced
  by the schema.** `max_level` is `INT NOT NULL DEFAULT 2`, so NULL cannot mean
  "unset" — binding one would violate the constraint and fail the signup. Branch
  3 therefore drops the column from its INSERT and lets the default supply it,
  which is the only legal spelling of "nobody has answered this". The nine
  columns Step 0 added already declare defaults that *are* blank (`'{}'`, `FALSE`,
  NULL), so they were already reached by not being named — which is also what
  keeps `link-user.ts` clean for `tests/test_phase3_columns.py`'s sweep. The
  INSERT is now `(user_id, cities, keywords, exclusions)`, and a test asserts that
  column list exactly.

  **A blank row cannot be saved through `PUT /api/preferences`, and that is
  correct rather than a bug.** That route refuses an empty `cities` or `keywords`
  because either matches no job at all. The row never meets it: branch 3 inserts
  directly, so signup does no validation and cannot fail on the seed, and
  `onboarding_completed = FALSE` holds the account on `/onboarding` until a person
  answers. The editor renders its city checkboxes from `CITY_LABELS` rather than
  from the stored list, so an empty `cities` is fully selectable — there is no
  dead end where a blank account cannot pick its way out.

  `npm run onboarding-check` carries the proof: 32 assertions green against the
  live database, including a direct read of the nine columns `loadPreferences`
  does not select (a claim about the DATABASE, which no amount of reading
  TypeScript could check). The operator's row was snapshotted before and after —
  43 keywords, 3 cities, 14 exclusions, `max_level` 2, `updated_at` unmoved.

  Deliberately **not** here: the onboarding form and its fields, matching, the
  crawler, the watch list, and city labelling.

  `scripts/reset_unonboarded_prefs.py` is the manual half, in its own commit
  after being run — the same ordering 1b and 1g used, and the sibling of
  `reset_unonboarded_keywords.py` one slice later. A seeding fix reaches only the
  accounts created after it, and the one account sitting mid-onboarding had been
  created while the seed still supplied the three cities and fourteen exclusions.

  Its guard is the 1b script's, widened to three value tests
  (`onboarding_completed = FALSE` **and** `cities` **and** `exclusions` **and**
  `max_level` each still exactly the seeded value, ordered equality throughout).
  **The second clause is not theoretical on this database**, which is the detail
  worth keeping: the operator kept the install's defaults, so its three values
  match the seed byte for byte and the value tests ALONE select it. The preview
  therefore prints what each clause matches in isolation — 1 row for the gate
  alone, **2 for the values alone**, 1 for both — so "the first clause is what
  excludes the operator" is a number rather than a paragraph. On its own, clause
  2 would have blanked the filter the operator's digest runs on, and an empty
  city list matches no job at all: a quiet inbox rather than an error.

  **`max_level` is matched but never written**, the one asymmetry in that file.
  A genuinely new account HAS `max_level` 2, because the seed omits the column and
  the `NOT NULL DEFAULT 2` supplies it — so writing anything would move the row
  away from what a fresh signup gets. The nine Step 0 columns needed nothing
  either, checked rather than assumed: they were already blank on that account,
  having been added after it was created. The script does not name them, because
  a statement clearing an already-blank column can only ever do harm.

  Run live before the commit: 1 row written, `cities` 3 → 0 and `exclusions`
  14 → 0, `keywords` already 0, `max_level` still 2. The operator unchanged at
  43/3/14/2. `updated_at` unstamped on both rows and asserted unmoved by id
  afterwards — the screen shows it as "last saved" and this claims no edit
  anybody made. A second preview matches 0 rows.

- **2026-08-08 — Phase 3 Slice 2. The experience ceiling becomes a per-account
  range.** `MAX_MIN_EXPERIENCE = 2` is gone from both languages. `is_match` now
  reads three columns Step 0 created and left inert — `exp_range_low`,
  `exp_range_high`, `show_no_experience_jobs` — and the constant's own comment had
  predicted exactly this ("if it ever needs to be editable it becomes a column,
  exactly like max_level did"). It was **deleted rather than left unread**, which is
  the `jobs.notified` argument applied to a name in Python: a value written by
  nobody and read by nobody is worse than absent.

  **No migration.** The columns existed and the operator's row was already
  backfilled, so this slice is pure code — the first one in a while that handed
  over no manual database step. That is Step 0's expand half paying off.

  Three branches, and the ORDER of the first two is load bearing:

  ```
  range unconfigured (either end NULL)  ->  match NOTHING
  min_experience NULL                   ->  keep iff show_no_experience_jobs
  otherwise                             ->  keep iff low <= min IS <= high
  ```

  **The first draft had those two the other way round, and a test caught it.** With
  the NULL-minimum branch first, an account with no range but the toggle on keeps
  every job whose description states no minimum — 7,809 of 7,880 open rows — so
  "matches nothing" silently meant "matches almost everything". `lib/jobs.ts` had
  the order right, so the SQL prefilter would have excluded rows the matcher kept:
  the prefilter narrower than the rule it prefilters for, which is the one thing
  that file may not be. Both languages now check the range first.

  **The install's behaviour is unchanged, and that is measured.** `(0, 2)` with the
  toggle TRUE reproduces `NULL or <= 2` exactly, because `min_experience` is never
  negative — `{0,1,2}` and "at most 2" select the same rows, and the range form is
  marginally *safer* since it would also exclude a negative. `match-parity` reports
  **38 before and 38 after**, same 18 companies, and
  `preview_digest --dry-run --ignore-notified` independently agrees at 38.

  Two things changed in kind. The rule can now drop a posting for being too
  **junior**, which a ceiling could not express (zero live rows affected: nothing
  sits below 0). And **only the minimum is stored**, so a job whose advertised band
  overlaps the range from below is dropped — a "3–6 years" posting is `min = 3` and
  an account looking for 5–8 never sees it. The symmetric rule was specified
  deliberately over the alternative `min_experience <= high`; the cost is on the
  record in a test that has to be deleted to change it.

  **An unconfigured range matches nothing rather than falling back to `(0, 2)`.**
  A fallback would hand a stranger the operator's answer, which is what Slices 1b
  and 1c spent two commits removing from the other four fields. It excludes rather
  than raising, because `run_fan_out` catches only `DigestError` and an exception
  would take every account's digest down over one blank row. And it is unreachable
  for an onboarded account by construction: `PUT /api/preferences` refuses an
  incomplete range and is the only writer of these columns *and* of
  `onboarding_completed`, so **onboarded implies configured** —
  `savePreferences`' parameter type is `number`, not `number | null`, so the one
  writer *cannot* write a NULL, and `npm run onboarding-check` asserts the resulting
  row against the live database.

  `years_of_experience_held` is wired to the **form only**. It is informational and
  never filters, and that is a property rather than a promise: it is absent from
  both `Preferences` value objects and from `PREFERENCES_SQL`, and
  `tests/test_phase3_columns.py` asserts the name appears in no matcher in either
  language — over `tokenize`d source, because the first draft of that guard used a
  line-prefix heuristic and tripped on the docstring that *states* the guarantee.

  **The onboarding form gained an Experience panel**, and one deliberate
  asymmetry in it. The range renders BLANK for a new account (a pre-filled `0-2`
  would be the operator's answer smuggled back in) while the toggle renders
  pre-checked **ON** — "unknown means keep" is this project's documented inclusion
  bias, not a personal preference, and a new account whose first digest is empty for
  an unexplained reason is the worse failure. The column stays FALSE until a real
  save, so it is a suggested answer rather than a smuggled default; the hint states
  the 99% that makes it matter. A blank range blocks Save, in the browser and at the
  route, exactly as an empty `cities` or `keywords` does.

  Verification: **997 pytest / 1 skipped**, typecheck clean, `match-parity` 4/4
  variants agreeing (38 / 45 / 18 / 0), `onboarding-check` 40 assertions,
  `ui-check` including 11 new ones over the shipped panel, `badge-check` green.
  Seven mutations were run against the new guards and all seven were caught —
  including the two decisions this slice turns on (the pre-checked toggle, the blank
  range) and the prefilter-narrower-than-the-matcher case, which `badge-check`
  catches as 38 → 20.

  `tests/conftest.py`'s two preference-seeding helpers now name the three columns.
  That is not housekeeping: the SQLite mirror declares
  `show_no_experience_jobs NOT NULL DEFAULT 0` and the range ints as NULL, so an
  unnamed row would have arrived unconfigured-with-unknowns-hidden and every test
  that seeds a filter and expects an ordinary unenriched job to match would have
  failed for a reason about the fixture.

- **2026-08-10 — Phase 3, cities Slice B. The selection becomes the filter.**
  Slice A grew the job side from 5 labels to 26 and deliberately left the matcher
  alone. This is the other half: `preferences.selected_cities` and
  `preferences.all_india` — created empty by Step 0, inert ever since — are now the
  city gate, in both languages and in the SQL prefilter.

  **No migration and no DDL.** The columns existed and
  `scripts/backfill_onboarding_prefs.py` had already written
  `selected_cities = {blr,pune,hyd}` onto the operator's row, so this slice is pure
  code — the second one in a row that handed over no manual database step, and the
  expand half of Step 0 paying off twice.

  Three branches over two columns, replacing one membership test:

  ```
  remote posting, or all_india  ->  anywhere except non-india
  otherwise                     ->  membership of selected_cities, minus two labels
  ```

  **`cities` is kept, still written, and no longer decides.** It is the fallback
  `Preferences.effective_cities` reads when `selected_cities` is empty, which is
  the state of any row that predates the backfill; reading only the new column
  would have silently emptied such an account's digest. It is a fallback to *this*
  account's own previous answer rather than to somebody else's, which is what
  makes it different in kind from the seeded-defaults failure 1b and 1c removed —
  and it cannot re-open the blank-account hole, because a new row has both columns
  empty. Dual-writing is also what makes the slice a no-op against the deployed
  digest, which still reads `cities`. The contract slice drops all of it.

  **Fail-closed, which is the decision this slice turns on.** `india-unclear` and
  `non-india` can never match an explicit selection. The first is a statement about
  our confidence rather than about a location, so it belongs to the all-India
  switch; the second is what all-India excludes by definition, and a selection able
  to match it would mean the narrow filter admitting rows the wide one refuses.
  `other-india` is NOT excluded — it names a real Indian place. Enforced three
  times (the picker does not offer them, the route refuses them, the matcher
  re-checks the label), because only the third holds for a row edited by hand.

  **Umbrellas expand at SAVE time.** Ticking "All Mumbai areas" stores the four
  members, so neither matcher learns that a region is a thing and the gate stays
  vocabulary-blind — the property that let the label set grow from 5 to 26 without
  either matcher changing. The cost is on the record: a city added to a group later
  does not reach accounts that already saved. The editor re-derives the umbrella as
  "all members ticked" and renders indeterminate, so such an account is visibly one
  save away from current rather than silently stale.

  **The remote branch is built and INERT, and this is the part not to misread.**
  A fully-remote posting is exempt from the city gate (never from the country
  floor). `jobs.work_mode` is NULL on all 9,703 stored rows because the parser is
  the next slice, so `is_remote` is False for every job and the branch cannot fire.
  It is built now so the work-mode slice turns it on by populating a column rather
  than by editing a rule. `match-parity` prints `0 of 7,869 rows non-NULL` so a
  green matrix is never read as coverage of it, and
  `test_the_remote_branch_is_built_but_inert` asserts both halves over synthetic
  rows — that the branch works, and that an unparsed row cannot reach it. NULL
  reads as not-remote rather than as unknown-means-keep: the branch RELAXES a gate,
  so the inclusion bias that governs descriptions is exactly wrong here.

  **Behaviour unchanged, and measured.** `match-parity` reports **38 before and 38
  after** over 7,869 live rows × 7 filters, and `preview_digest --dry-run
  --ignore-notified` independently agrees at 38 (blr 33 / hyd 3 / pune 2). The
  operator's `selected_cities` equals their `cities`, so the gate reduces to
  today's membership test either way — which is why the reproduction holds whether
  it reads the new column or the fallback.

  Three parity variants added: all-India, a blank city answer (**asserted empty**,
  like the unconfigured range — both languages agreeing on a wrong answer would
  pass a comparison), and the Mumbai umbrella's stored expansion.

  `lib/jobs.cityClause` is the SQL half and has one load-bearing character:
  `IS DISTINCT FROM` rather than `<>`. `j.city <> 'non-india'` is NULL when the
  city is NULL, so the row is excluded, while the matcher's `?? ""` keeps it —
  the prefilter narrower than the rule it prefilters for, which is the one thing
  that file may not be. Zero live rows have a NULL city, so it would have shipped
  green.

  1308 tests pass (26 new in `tests/test_city_selection.py`), typecheck clean,
  `ui-check` +20 assertions over the shipped picker, `onboarding-check` and
  `badge-check` green. Five mutations run and five caught — and the first pass
  found a real hole: two tests parametrized over
  `CITY_SELECTION_NEVER_MATCHES` silently shrank when the constant did, so the
  membership is now pinned literally.

  Deliberately **not** here: the work-mode parser, `work_modes`, `salary_bands`,
  the watch-list picker, and the Slice A relabel — see *Pending*.

- **2026-08-10 — Phase 3, the work-mode slice. The remote branch stops being inert.**
  Two halves, as the cities slices were: a parser that fills `jobs.work_mode`, and
  `preferences.work_modes` — created empty by Step 0 — becoming a filter. Together
  they turn on the relaxation cities Slice B built and could not fire.

  **`work_mode` is on the `is_pm_role` rail**, not the `city` one. It is written by
  `notify/store.SAVE_DESCRIPTION_FACTS_SQL`, once, when a description is read, and
  by nothing else — `tests/test_phase3_columns.test_only_the_enrichment_statement_
  may_write_work_mode` asserts both halves. The crawl deliberately may not write
  it: `UPSERT_SEEN` runs hourly on every re-sighting and would overwrite a
  description-derived verdict with a guess from a location string, so a job read as
  remote on Monday would be silently un-remoted by Tuesday.

  **Two tiers, and the first one is not a parser.** Three of the five live ATSes
  publish the answer, verified against real boards rather than documentation:
  `ashby.workplaceType` (`OnSite`/`Hybrid`/`Remote`), `lever.workplaceType`
  (lowercase), `smartrecruiters.location.remote`/`.hybrid` (two booleans, both
  FALSE being a real onsite). All three ride in payloads already fetched, so the
  structural tier costs nothing. **Greenhouse and Workday publish no such field at
  all** — checked against postings whose location says "Remote - US" — which is
  5,589 of 7,860 open rows and why the prose tier has to exist.

  **The prose tier is deliberately strict, and the asymmetry is the reason.** A
  wrongly-onsite job is a quiet miss; a wrongly-remote job **bypasses the city
  gate** and can arrive from anywhere in the country on one misread sentence. So
  only unambiguous statements about *this posting* count — `fully remote`,
  `100% remote`, `this role is remote`, `work from anywhere`, `Location: Remote`.
  Rejected with reasons on the record: `remote-first` and `all-remote` (statements
  about the company — every GitLab JD carries one), `remote-friendly`, `flexible`,
  `distributed`, and **`wfh` / `work from home`**, which attach to benefits far
  more often than to arrangements. Two of the three remote verdicts in the first
  live dry run came from bare `WFH` and neither survived review.

  On top of framing sits a **masking veto** — `_mask` blanks technical and
  incidental uses the same length, exactly `_mask_experience_numbers`' idiom — so
  "remote servers", "manage a remote team", "remote procedure call", "hybrid
  cloud" cannot be read as arrangements, *and* a description can still say both
  things in two sentences and be read correctly.

  **The dry run caught a real false positive, which is what it is for.** The first
  live pass added one job: a Stripe posting, location field `na`, matched on
  "Experience in **fully remote** work environments" — a requirement about the
  *applicant's history* in a requirements bullet. The veto grew a
  candidate-history branch (`experience|comfortable|thrive|proven|track record …
  remote`, bounded so it cannot cross a sentence) and that line is now a test.

  **The enrichment seam had to widen, or the feature was decorative.**
  `crawler/enrich.py` narrowed by `title_stage_ok`, which **includes `city_ok`** —
  so a job the relaxation would rescue is exactly a job that never earned a
  description read, and `work_mode` could never become `remote`. `notify.match.
  worth_a_description` is the fix: the same tests minus the city gate, plus
  `india_funnel(location)` as the country floor, since a `non-india` posting can
  never match whatever its arrangement is. That is the difference between 310 extra
  candidates and 24. It reads the **location string**, not `jobs.city`, so it is
  correct on a database whose labels predate the current vocabulary — which this
  one is.

  **NULL is read as onsite and never written.** `DEFAULT_WORK_MODE` collapses it at
  the moment of comparison; the column keeps saying nothing. "We read it and it
  said nothing" and "we never looked" stay different facts on the row — the
  `jobs.notified` lesson applied *before* the mistake instead of after it. ~9,675
  rows are still NULL and are matched exactly as they were before this slice.
  `lib/jobs.workModeClause` therefore says `COALESCE(j.work_mode, 'onsite') =
  ANY(…)`; without it the prefilter would be narrower than the matcher and Home
  would drop to a handful of rows.

  **The field is fail-closed on blank**, the fourth member of the family with the
  unconfigured range and the blank city answer, and unreachable for an onboarded
  account by the same construction — `PUT /api/preferences` refuses an empty
  selection and is the only writer of the column *and* of `onboarding_completed`.
  `DEFAULT_WORK_MODES` is all three, mirroring what
  `backfill_onboarding_prefs.py` wrote; "blank matches nothing" lives in
  `from_row`, not in the constant. Getting that backwards made a bare
  `Preferences()` match zero jobs and cost 85 red tests, which is the guards
  working.

  **`scripts/backfill_work_mode.py`** is prove-before-commit, shaped like
  `relabel_cities.py`: projected distribution, a **marker report** (every non-onsite
  verdict grouped by the phrase that produced it — the reviewable unit is the
  marker, not the row), a **blast radius** per account computed with the whole
  matcher, the **added-by-remote-relaxes-city list** with the description snippet
  behind each verdict, and a lifecycle fingerprint over every column except
  `work_mode`. Its fingerprint window is the **write**, not the run: the first live
  pass reported a MISMATCH because the hourly crawl landed during three minutes of
  fetching, which is a fact about this database rather than about the UPDATE.

  Applied: **102 rows written — onsite 97, hybrid 5, remote 0**; 9,675 stay NULL.
  Fingerprint MATCH. `match-parity` **34 before and 34 after** over 7,860 live rows
  × 12 filters (the live total was 38 a day earlier; the board churned, and the
  slice moved it by nothing). `onsite only` reports **33**, which is the mode gate
  proving it is not a no-op.

  **No live row is remote, so `match-parity` gained a synthetic probe.** Four
  fabricated rows, judged by both implementations and inserted nowhere, prove the
  branch fires (remote + chennai matches), that the country floor holds (remote +
  non-india does not), that NULL is not remote, and that an account which did not
  tick `remote` never sees one. The old `0 of N non-NULL` note became a **failing
  gate**: zero now means the backfill was never applied.

  1,517 pytest pass (72 new in `tests/test_work_mode.py`), typecheck clean,
  `onboarding-check` and `badge-check` green.

- **2026-08-10 — Phase 3, the salary slice. The last onboarding field, and the
  first one that is honestly inert.** Two halves as usual — a parser that fills
  `jobs.salary_min` / `salary_max`, and `preferences.salary_bands` becoming a
  filter — and a third thing the brief did not ask for and the data demanded.

  **THE MEASUREMENT CAME FIRST AND IT SHOULD BE READ BEFORE ANYTHING ELSE HERE.**
  Before a line was written, 36 of the 102 enrichable rows were fetched through
  the production path and grepped for money: **36 read cleanly, ZERO stated a
  number, 22 mentioned salary/compensation/CTC without one.** Indian postings on
  these boards do not publish pay. So this slice ships a correct filter over a
  column that is empty and will stay empty, and success was defined up front as
  *correct + match-parity unmoved + the synthetic probes prove the gate fires* —
  explicitly NOT "the digest changed". It did not change, by one job.

  **`jobs.salary_checked` is the column Step 0 did not know it needed**, and it is
  the `jobs.notified` lesson applied BEFORE the mistake rather than after it. Step
  0's comment said "NULL = not yet parsed", which is only true while nothing
  parses. The moment a parser runs, NULL means either "read it, states no pay"
  (100% of this board) or "nobody has looked" — one column, two facts, told apart
  by nothing. `work_mode` dodged this by having `desc_checked` beside it; the
  salary pair could not borrow that, because **87 of the 102 rows were read
  cleanly before this parser existed**, so `desc_checked` is TRUE on rows no
  salary parser has ever seen. The flag decides no match (an unpriced job shows
  either way) — the distinction is kept on the row where it is free, not enforced
  in the filter where it would change an inbox. It also makes the backfill
  idempotent in COST, not just in effect.

  **The structural tier is real, built, and empty.** Of the nine ATSes, exactly
  two publish a comp field — `ashby.compensation.summaryComponents` (only with
  `?includeCompensation=true`, which is why `notify/description._BOARD_URLS`
  differs from `fetchers/tier1.py`'s copy by one query parameter) and
  `recruitee.salary`. Verified live rather than from documentation: **greenhouse,
  workday, smartrecruiters and lever publish nothing**, and those four are 100% of
  the enrichable rows. Non-INR is refused rather than converted, in both tiers —
  an FX rate would freeze into a column nothing re-derives, and $180k converts to
  ~150 LPA and lands in `50+` looking like the best job on the board while being
  in Seattle.

  **THE ANCHOR IS THE MAIN GUARD, AND IT IS STRUCTURAL RATHER THAN A BLOCKLIST.**
  Nothing is read as pay without a currency (`₹`, `Rs`, `INR`) or a unit (lakh,
  lac, crore). That single rule makes thirteen real strings from the live sample
  invisible — `30,000+ employees`, `200,000 people`, `3000+ strong team`, `2,500
  companies`, `the 200,000th candidate`, `Founded in 1871`, `+1 408-536-3015`,
  `5 years of experience` — where a list of things to avoid could always be
  incomplete. Six vetoes then handle real money that is not this job's pay:
  equity/ESOP, funding/revenue/valuation, benefits, foreign currency,
  **application-form questions** (structurally guaranteed on all 46 Greenhouse
  rows, because `_greenhouse_description` joins `questions[].label` in on purpose)
  and hourly/daily rates. Ambiguity resolves to silence — **two disagreeing
  figures produce nothing**, which is the deliberate inverse of
  `detect_min_experience`'s `min(found)`: there the smallest number is the answer
  that KEEPS the job, and here no number is safe.

  **The dry run caught a false positive, which is the entire reason it exists.**
  The first live pass priced five PhonePe postings at 150 LPA from *"an Annualized
  Total Payment Value (TPV) of over INR 150 lakh crore"*. Every guard was working:
  the anchor was honestly satisfied, and the figure landed inside the plausibility
  band — because the parser had matched `INR 150 lakh`, stopped, and never seen
  the word that changes the magnitude by seven orders. **`lakh crore` is one unit
  and it is not lakhs.** The fix is the number's own shape (every lakh form now
  refuses a trailing `crore`, so the compound matches nothing at all) rather than
  a veto having to recognise "TPV" — though the funding veto learned that too.
  Only a person reading the snippet beside the number could have caught it, which
  is what the SAMPLE section of the report is for.

  **`salary_bands` fails OPEN, and it is the first member of that family to break
  the pattern.** An unconfigured experience range, a blank city answer and an
  empty `work_modes` all match NOTHING; an empty `salary_bands` filters nothing.
  Four reasons, in `notify/match.salary_ok`: the empty-digest failure the other
  three guard against is unreachable here; fail-closed would contradict the rule's
  own first branch (silence from the EMPLOYER shows the job, so silence from the
  READER must not delete it); the job side is silent almost always, so blank is a
  resting state rather than an unanswered question; and consequently `PUT
  /api/preferences` does **not** require it, so **"onboarded implies configured"
  does not extend to this column**. That last one is a real, deliberate hole in a
  construction three other columns depend on, and it is stated rather than left to
  be discovered.

  **`match-parity` went 12 → 15 variants and 4 → 10 synthetic probes**, and one
  existing assertion had to change shape. The fail-closed work-mode check read
  `perVariant[VARIANTS.length - 1]` with a comment explaining that indexing from
  the END made it safe against insertions ABOVE — and this slice inserted three
  variants BELOW it, silently re-pointing it at the blank-SALARY variant, which
  must behave the opposite way. Every reference now goes through `variantIndex(label)`.
  The three salary variants are asserted **equal to the live total** rather than
  empty, which is the inverse of how the other three blanks are asserted and is
  the fail-open rule stated as a test. The coverage line is a **report, not a
  gate**, deliberately unlike `work_mode`'s: zero priced rows is the expected
  state, and a check nobody can make green is one people learn to ignore.

  Applied: **102 rows marked `salary_checked`, 0 priced.** Fingerprint MATCH.
  `match-parity` **34 before and 34 after** over 7,863 live rows × 15 filters.
  1,468 pytest pass, 1 skipped (82 new in `tests/test_salary.py`), typecheck clean,
  `onboarding-check` green.

- **2026-08-11 — Phase 3, the watch-list slice. The picker, and the write path
  the app never had.** `user_companies` has existed since Phase 1c and the digest
  queue has read it since 1e, and until this commit **nothing in the web app
  wrote it**: `scripts/track_companies.py` was its only creator. So a new account
  watched nothing, its queue's first clause was false for every job on the board,
  and its digest was empty forever — while Home showed it all 50 catalog
  companies with per-user match counts on them, as though those were its
  companies. The landing page has said "tell us the companies you'd like to
  watch" the whole time.

  **`lib/watchlist.ts` is the writer and it is a PORT, not a new design.**
  `watchCompany` is `db/watchlist.track_companies` for one id: the pair
  (`ON CONFLICT DO NOTHING RETURNING`) and the history suppression
  (`db/notifications.SUPPRESS_COMPANY_HISTORY_SQL`) in **one transaction**, the
  second running **only on a genuine insert**. Both statements are compared
  normalised against the Python by `tests/test_watchlist_write.py`, which also
  asserts the suppression is not scoped to open rows, that both statements run on
  the transaction handle rather than the pool (bound to `sql` inside `sql.begin`
  they would run on another connection and the atomicity would be decorative),
  and that the already-watching branch RETURNS rather than falling through into
  the suppression it exists to skip. Seven mutations were run against those
  guards and all seven were caught.

  **The number this slice turns on: 1,180 and 0.** Adding Continental — 1,180
  stored jobs, 212 of them open and not back catalogue — to a fresh account
  recorded 1,180 rows as already-seen and left that account's queue at **zero**.
  Without the second half those 212 would have been its first email.
  `npm run watchlist-check` is that run, against the live database, plus: a
  re-add suppresses 0, an untrack leaves the suppression rows alone (otherwise a
  toggle twice re-delivers everything in between), and the sixth company is
  refused with **nothing written**, because the refusal is a throw inside the
  transaction that had already inserted the pair.

  **Home and Jobs moved to the watch list; the catalog moved to `/watchlist`.**
  The rule is not "narrow everything" — it is that a screen printing a per-user
  number has to follow the person, because a "for you" count over a company you
  do not watch counts jobs that can never arrive. `GET /api/companies`, the Add
  flow and the picker itself still see the shared catalog.

  **"Watch up to 5 companies" was on the pricing card and enforced nowhere.** It
  is now `FREE_WATCHLIST_CAP`, read from `public.users.plan`, checked inside
  `watchCompany`'s transaction — with a test asserting the constant and the
  landing copy state the same number, because a cap raised in one and not the
  other is a promise the product quietly stops keeping. It refuses **additions
  only**: the operator watches 50, put there by the Phase-1c backfill two phases
  before any limit existed, and a cap that trimmed a watch list would silence a
  digest that has been running for months.

  **Onboarding gained a fifth required answer**, fail-closed like the city
  selection: `PUT /api/preferences` refuses a save from an account watching
  nothing. Auto-watching the catalog instead would be the seeded-defaults mistake
  1b and 1c removed, one table over. The picker sits above the editor on
  `/onboarding` and commits immediately (the suppression is a transaction, not a
  staged edit) while the filter still waits for Save — the asymmetry the resume
  panel already established on that screen.

  **Nothing about the operator's account moved, and that is measured.**
  `match-parity` **34 before and 34 after** over 7,852 live rows × 15 filters;
  they watch every catalog company, so their Home is the same fifty rows either
  query produces — asserted in `ui-check` rather than assumed. 1,547 pytest pass
  (69 new), typecheck and `next build` clean, `ui-check` +24 assertions over the
  shipped picker and the three new 401s, `onboarding-check` and `badge-check`
  green.

  Deliberately **not** here: `companies.tracked` (still sealed, still the
  crawler's), `is_active` (a new account still cannot receive email until a
  sending domain is verified — see *Pending*), the matchers, the crawler, and the
  Python watch-list path, which is unchanged and still the CLI.

- **2026-08-11 — Phase 3, the onboarding-form slice. Setup stops being the
  preferences editor with different copy.** Presentation only: not one field,
  validation rule, save gate or route behaviour changed, and that is the claim the
  slice rests on rather than a disclaimer attached to it. `PUT /api/preferences`
  is untouched, `savePreferences` is untouched, the Save button's `disabled`
  expression and its label chain are the same five terms in the same order.

  **What /onboarding was.** Every panel this app owns, stacked: the watch-list
  picker, then a resume drop zone, chips, an experience panel, three multi-selects
  and two more chip lists, ending in a button. Nine cards with no grouping and no
  statement of what the whole thing was for — and the button that refuses, with
  the reason written on it, at the very bottom of several screens of scrolling. A
  person could not see why Save was greyed without leaving the answer they were in
  the middle of giving.

  **What it is.** The same panels, grouped into six named sections in a deliberate
  reading order — resume and keywords, experience, cities, work mode, pay,
  companies — each with a heading that asks the question and a line saying what
  the answer does. Still ONE page: a stepper hides four answers behind a Next
  button while asking about the fifth, and every answer here is visible to every
  other one (how senior a job you want and what it pays are the same decision).

  **The save bar is sticky, and being LAST is what makes it work.** `position:
  sticky; bottom: 0` keeps an element pinned only while its own containing block
  is on screen, so a bar with a section under it scrolls away exactly when the
  form gets long enough to need one. That is why the watch-list picker is threaded
  THROUGH the editor as `extraSections` rather than rendered beside it, and why
  `ui-check` asserts the ordering over the shipped `OnboardingSetup` rather than
  over the editor alone — the property is invisible in either component's own
  markup and a mutation moving the bar above the picker was run and caught.

  **The bar lists what is missing, and lists nothing it decides.** Each entry
  restates one of the booleans the button already reads and links to the section
  that fixes it. One entry — an empty keyword list — is deliberately NOT a term of
  the button's gate: the route refuses it, so it genuinely blocks a save, but
  adding it to `disabled` would change what saves rather than what is said, and
  this slice changes nothing about what saves.

  **The order reversed, and the old reason is on the record because it was not
  wrong.** The picker used to be first, on the argument that "no keyword matched"
  and "you watch nobody" are indistinguishable on an empty dashboard. The save bar
  answers that better — it names every missing answer at once, permanently — and
  what the old order cost was the opening: fifty company names as the first thing
  a person sees, before anything asked what kind of job they want.

  **THE NAV IS HIDDEN ON /onboarding, discharging the *Pending* item Slice 1b
  left.** That note weighed two ways of telling the layout — a query per
  navigation, or threading the flag out of a page into its own layout — and both
  answer "is this account onboarded". The chrome's question is not that one. It is
  "is this the setup screen", which `usePathname()` already answers on the client
  for free, so `app/layout.tsx` still reads no database and the gate is untouched.
  **Sign out stays on both branches**: removing the navigation from a screen a
  person is held on is only acceptable while there is still a way off it.

  `ONBOARDING_PATH` moved from `lib/guard.ts` to `lib/labels.ts` for the reason
  that file exists — `Nav` is a client component, and importing a VALUE from
  `guard.ts` would drag `lib/preferences` and postgres.js into the browser bundle.
  `guard.ts` re-exports it, so the gate's destination and the route the tabs
  vanish on are one string; `onboarding-check` asserts the equality and
  `tests/test_dashboard_auth.py` was widened to read the literal from wherever it
  is declared while still requiring `guard.ts` to name it.

  Verification: **1,547 pytest / 2 skipped**, typecheck and `next build` clean,
  `ui-check` **112 → 155 assertions** (the nav on three routes, the six sections,
  the bar and its blocker list, and /onboarding's composition end to end),
  `onboarding-check` +1 and green — a blank account is still held at the gate and
  its first save still releases it. Two mutations run, two caught: deleting the
  nav's hide (6 failures) and moving the save bar above the picker (1).

  Deliberately **not** here: matching, the parser, `lib/preferences`, every `/api`
  route, the crawler, and the landing page's black theme — this is the app
  surface, styled from the same `globals.css` tokens as every other screen.

- **2026-08-11 — Phase 3, the onboarding reskin. The setup form gets a designed
  surface, and two fields leave it.** A RESKIN OF A WORKING SCREEN, not a rebuild:
  every field component, every setter, every validation rule, the keyword cap, the
  city umbrella and All-India exclusivity, the watch-list cap and its
  history-suppression transaction, the sticky-save gate's five terms and the `PUT
  /api/preferences` body are what they were. The save that flips
  `onboarding_completed` is untouched, and `api/`, the matchers, the parsers and
  the crawler are not in the diff at all.

  **The design is a de-bundled static reference** kept at
  `dashboard/design-reference/showstencil-onboarding-reference.html` — warm paper
  (`#efece3`), white cards, `#b9a184` accent, Instrument Serif headings over an
  IBM Plex Mono eyebrow. Both faces were ALREADY committed as latin-subset woff2
  in `src/fonts/` for the landing page, so the reskin added no font files and the
  page makes no `fonts.googleapis` request; the production build emits three
  `.woff2` and contains no Google reference.

  **SCOPED, THE WAY landing.css IS.** `app/setup.css` is scoped under two classes
  rather than merged into `globals.css`: `.setup-form` (the editor — rendered by
  BOTH /onboarding and /preferences, because one component with two looks is how
  the two drift) and `.setup-page` (the paper, the hero and the 01-06 numbering,
  which /onboarding alone renders). Home, Jobs, Watchlist and Add are untouched.
  The palette is installed by REBINDING the app's own token names inside the scope
  — `--panel`, `--line`, `--accent` and the rest — so every existing `.panel`,
  `.msg`, `.chips` and `.switch` rule repaints in one place, and `color-scheme` is
  pinned light for the reason the black landing page pins dark.

  **The section numbers are a CSS counter, not a prop.** Setup is a sequence
  somebody walks through once; /preferences is the same questions as a settings
  screen, where numbering implies an order that no longer means anything. Deriving
  them from the DOM also means adding a section cannot leave a number behind.

  **EXCLUSIONS AND max_level ARE OFF THE SETUP SCREEN, AND THE VALUES STILL
  TRAVEL.** `fineTuning={false}` hides the section on /onboarding; /preferences
  leaves it at the default and is now the only place either is editable. The state
  and `save()`'s body are unchanged, which is the whole of what makes it a hidden
  SECTION rather than a dropped FIELD — and the failure mode is two-sided: `PUT
  /api/preferences` REQUIRES `max_level`, so omitting it would refuse the one save
  that finishes onboarding for every new account (the Slice 1b cap collision,
  arriving through a different field), and an omitted `exclusions` cleans to `[]`,
  which equals the seed today and would silently erase a real list tomorrow. A
  control nobody is shown must not become an answer nobody gave.
  `onboarding-check` §6a is the proof, and it passes the row's OWN seeded values
  through rather than literals, so it asserts a pass-through rather than a
  coincidence.

  **The success screen replaced an immediate redirect, and the ordering is the
  point.** `redirectOnSave` used to call `router.refresh()` and `router.replace()`
  the instant the response carried `onboarding_completed`. Both calls now sit
  behind the "Go to dashboard" button: refresh() re-renders THIS route on the
  server, where the page redirects to "/" the moment the flag is TRUE, so firing
  it on arrival would tear the screen down in the tick it went up. Nobody can be
  stranded there — the flag is already written, so a reload lands on the
  dashboard.

  **The city grid reads "Bengaluru" and stores `blr`.** `CITY_DISPLAY_NAMES` is
  presentation and the lookup is one-way; the slug is on every control's `title`
  and in its accessible name, and a label missing from the map renders as itself.
  The two umbrellas moved out of the `fieldset` into the design's pill row, so
  they now carry their own `disabled` — without it, All over India would leave two
  live controls writing a selection it ignores.

  **The picker gained a second presentation, not a second picker.**
  `variant="search"` is /onboarding: a search box, six results, chips for what you
  picked. `browse` is the default and /watchlist is unchanged. Both call the same
  `toggle()` against the same route under the same cap.

  The save bar stays `sticky` rather than the design's `fixed`, deliberately:
  /preferences renders the delete-account section below it, and a fixed bar would
  hover over the one control in this app that cannot be taken back.

  Verification: **1,547 pytest / 2 skipped**, typecheck and `next build` clean,
  `ui-check` 155 → 163 assertions (the new section copy, the friendly city names
  with their slugs, the disabled umbrellas, fine-tuning present on /preferences
  and absent from setup, a seeded account still savable, and the success screen
  absent before a save), `onboarding-check` +3 and green.

**Pending**

- **The Slice A relabel has never been applied to live Postgres, and the deployed
  crawler is nine commits behind.** `main` is ahead of `origin/main` by ten
  commits, and GitHub Actions runs the pushed tree — so the hourly crawl is still
  the pre-Slice-A labeller, still writing the old five labels. Live `jobs.city` is
  `other` 7,273 / `blr` 1,782 / `hyd` 283 / `india-unclear` 242 / `pune` 123, and
  `other` is a label neither runtime's vocabulary admits any more. The Slice A
  entry above describes a relabel that ran against a dry run, not against this
  database.

  Two consequences, and the second is the sharp one. **21 of the 26 labels match
  zero live rows**, so ticking `mumbai` or `noida` returns nothing although those
  jobs demonstrably exist — inside `other`. And **all-India is wrong in the
  dangerous direction**: it is the complement of `non-india`, no live row carries
  `non-india`, so it admits the entire open board including every foreign posting
  sitting in `other`. `match-parity`'s all-India variant reports 343 against the
  live row's 38, and that gap is the measurement.

  It cannot be fixed by relabelling alone: `crawler/store.UPSERT_SEEN` sets
  `city = $5` in its `ON CONFLICT DO UPDATE`, so the deployed old labeller rewrites
  every re-sighted row within one hourly cycle. The order is **push, then relabel**
  — and `relabel_cities.py`'s two gates (residue, blast radius) are what say
  whether the relabel is safe to apply when it runs.

- **The Add flow can still turn `tracked` back ON, and can rename a company.**
  The residual half of Slice 1a, deliberately left. `POST /api/companies` →
  `lib/companies.upsertCompany` carries
  `ON CONFLICT (ats, token) DO UPDATE SET name = EXCLUDED.name, tracked = TRUE`,
  so any signed-in account that re-adds an existing company by name flips a
  deliberately-off row back on and overwrites its display name. It is a strictly
  smaller surface than the toggle that was removed — it can only ever set TRUE,
  so it cannot stop a crawl or silence a digest, which is why it was not folded
  into that fix — but it is the same shape: a shared-catalog write reachable by a
  caller whose identity nothing checks. Fixing it means deciding what a re-add
  should mean for a row somebody switched off on purpose, which is the question
  `discover.TRACK_SQL` already answered for the sweep with `DO NOTHING`.
- Work the `discovered_companies` "try Workday" queue by hand with
  `scripts/add_workday.py` — the sweep cannot resolve a Workday site. **97**
  names carry the flag, out of 224 unresolved rows; 5 Workday tenants are
  tracked so far.
- **Tier-3 has no description route and no tracked company**, and those two facts
  are related: the gap has never bitten because nothing exercises it. If a
  `custom_jsonld` or `custom_sitemap` company is ever tracked, every one of its
  postings enters the digest with `min_experience` NULL — the second-stage filter
  silently off for that vendor, which is precisely the Step 7c failure. Slice 2
  narrowed that slightly without closing it: an account with
  `show_no_experience_jobs = FALSE` now drops those postings instead of receiving
  them. That is not a fix, and reading it as one would be a mistake — the install's
  own row has the toggle ON, so for the account that actually exists the leak is
  exactly as open as it was.
- **A swept-in company still reaches nobody's digest until somebody watches it.**
  The discovery sweep and the dashboard's Add screen both write `companies` and
  stop there, which remains the right default — auto-subscribing every account to
  whatever a sweep found is the archive dump with a different trigger. What
  changed with the picker is who can do something about it: `/watchlist` shows the
  whole catalog, so a newly-swept company is visible and one click from being
  watched, and `scripts/track_companies.py` is no longer the only way. The
  operator's own account still watches all 50 and is unaffected either way.
- **A new account is `is_active = FALSE`, so finishing onboarding still does not
  produce an email.** That is a different axis from this slice and deliberately
  untouched: `notify/digest.assert_self_send` refuses any recipient other than
  `MY_EMAIL` while `FROM_EMAIL` is Resend's sandbox sender, so a new account with
  `is_active` TRUE would enter the fan-out, fail, and turn the cron's "Send the
  digests" step permanently red for everybody. Verifying a sending domain (2d) is
  what flips it. Worth stating beside the watch list because the two together are
  what "a new user gets a digest" needs, and only one of them is now true.
- **Enrichment is scoped to the MY_EMAIL user's filter**, because
  `enrich_matches` is a crawl entrypoint and needs *a* preferences row to pick
  title-stage matches. Harmless with one account. With a second account whose
  keywords differ, their matches can reach their digest `desc_checked = FALSE` —
  `min_experience` NULL, and unknown means keep, which is the Step 7c shape
  arriving by a new route. The fix is a design choice (enrich over the union of
  active filters, or per user) that Phase 2 will re-frame, so it is written down
  rather than guessed at now.
- **Two comment references in `db/schema.sql` name scripts 1g deleted** —
  `scripts/backfill_seeded_flag.py` and `scripts/backfill_user_notifications.py`,
  both in the present tense ("is what then makes the column honest",
  "is the seed"). The git history has them, which is what the surrounding prose
  says; the sentences pointing at the working tree need re-tensing.

**Won't do (deliberate)**

- **Rotate the Supabase database password.** Decided against, rather than
  outstanding. The DSN lives in `.env` (gitignored and blocked by filename), in
  four GitHub Actions secrets and in Vercel's environment — rotating it means
  updating all three in lockstep, and a miss takes the hourly cron down silently
  until the next digest fails to send. The password has never been committed:
  the pre-commit guard blocks credential shapes, and the unpushed dashboard
  history was squashed so no blob carries one. Revisit only if it actually leaks.

**Done since this list was written**

- Deploy the dashboard to Vercel — **done.** It is deployed with Root Directory
  = `dashboard`, and the production OAuth redirect URI is registered.
- `scripts/reset_unonboarded_keywords.py` — **run.** The 1b manual half, and the
  evidence is stronger than the empty list it leaves behind, because an empty list
  is also what the 1b *seeding* fix produces for an account created after it. This
  account was created **2026-08-07 18:16 UTC**, about ten hours BEFORE that fix
  shipped (`822166c`, 2026-08-08 04:34 UTC), so it was seeded with all 43 and its
  keywords are now 0. And `preferences.updated_at` is still identical to the
  account's `created_at` — never stamped, which is that script's deliberate
  signature and rules out the other way the list could have emptied: a save
  through the screen sets `onboarding_completed` TRUE and moves `updated_at`, and
  the row is still FALSE.
- `scripts/reset_unonboarded_prefs.py` — **run** (`ea17589`), clearing the 1c
  residue on the same account. It went `cities` 3 → 0 and `exclusions` 14 → 0,
  with `keywords` already 0 and `max_level` left at the schema default of 2, so it
  now sits at exactly the row a brand-new signup gets. The operator's row is
  unchanged at 43/3/14/2 with `updated_at` unmoved. A second preview matches
  nothing. **No un-onboarded account now carries any part of the operator's
  filter**, which is what Slices 1b and 1c together set out to be true.

- **2026-08-10 — Phase 3, cities Slice A. The city vocabulary goes from 5 labels
  to 26, job-side only.** `jobs.city` could say `blr`, `hyd`, `pune`,
  `india-unclear` or `other`, and `other` was three facts wearing one name: an
  Indian city we had not listed, a confidently foreign posting, and a string no
  rule recognised. That is fine while the only question is "is this one of my
  three cities?" and useless the moment somebody asks "show me everything in
  India". So the collapse came out — `_classify` had always computed the
  distinction — and the city list grew to the 23 the dropdown will offer.

  **Built survey-first, on purpose.** `scripts/survey_all_locations.py` (new,
  read-only) dumped every distinct location string across 9,689 stored jobs with
  the verdict it gets *and the marker that produced it*, plus a token-frequency
  view, a fuzzy near-miss pass, and the **residue** — 590 rows across 150 strings
  that matched no marker in any table. The alias table was then built from those
  spellings rather than from English, and minimised computationally: each
  candidate marker was dropped and the whole 1,654-string corpus re-labelled,
  and it stayed only if a row's label changed. That killed 16 speculative Indian
  markers and 47 redundant foreign ones. `hubli`, `mangalore` and `belgaum` were
  named in the brief and rejected for carrying zero rows, as were all fourteen
  airport codes for the new cities.

  **Two bugs the survey found.** `ncr` sat in the Indian marker table and was
  matching the *Philippines'* National Capital Region — 15 LSEG rows classified
  as Indian, invisible while everything foreign shared `other` with Lucknow, and
  about to become `other-india`. And Workday's `locationsText` bare count form
  ("2 Locations") reached the no-marker fallback, caught by the new residue gate
  against `tests/data` rather than by review.

  **The three legacy spellings were kept and scanned first**, which is what made
  this safe to run live: `blr`/`hyd`/`pune` are what every `preferences.cities`
  holds, so renaming them would have meant rewriting every filter inside the
  relabel's own transaction. Instead the only rows the vocabulary can move are
  rows that were already outside every filter. Verified on the corpus before
  touching anything — 0 rows leaving those three labels, 0 entering.

  `india_funnel` was redefined as a **complement** (everything except
  `non-india`) rather than membership of a hand-listed set, because forgetting a
  24th city would make the Workday unscoped trim silently stop *storing* it.

  `scripts/relabel_cities.py` gained the two gates the migration needed and
  **refuses to apply on either**: a non-empty residue (a label from a fallback,
  which nobody can review) and a non-zero blast radius (a job crossing any
  user's `cities` — the entering direction has no `user_job_notifications` row,
  so nothing suppresses it and it would go out as news because a labeller
  changed).

  **Run live:** dry run clean, then 9,171 rows relabelled, lifecycle fingerprint
  MATCH over all 15 non-`city` columns. `npm run match-parity` returned **38
  before and 38 after**, and `preview_digest.py --dry-run --ignore-notified`
  the same 38 (blr 33 / hyd 3 / pune 2). Nothing moved.

  Distribution after: `non-india` 6728, `blr` 1774, `hyd` 283, `noida` 271,
  `india-unclear` 152, `pune` 123, `mumbai` 103, `other-india` 99, `gurugram` 88,
  `chennai` 44, and a long tail; `thane`, `mumbai-suburban`, `nagpur`,
  `madurai`, `mangaluru` and `belagavi` exist with zero rows because no board
  here writes them.

  TypeScript side: `CITY_LABELS` expanded to the same 26 in the same order (with
  a new test that parses `labels.ts` and asserts the two lists match — the
  runtimes share no code, so the list exists twice), `other` removed, and the
  `cleanList(body.cities, { max: 20 })` **silent truncation** fixed to refuse —
  26 labels against a cap of 20 meant an account ticking every box would have
  had six dropped and been told the save succeeded.

  Deliberately **not** in this slice: the city dropdown, `selected_cities`,
  `all_india`, and the matcher's city logic. `preferences.cities`,
  `DEFAULT_CITIES`, the schema seed and `discover.TARGET_CITIES` are all
  untouched. 1279 tests pass (268 new in `tests/test_city_vocabulary.py`);
  fifteen assertions that pinned the literal string `"other"` were rewritten to
  the invariant they actually meant — a regression test pinned to a spelling
  stops testing its regression the day the spelling changes.

---

## Conventions

- **No hardcoded personal data, anywhere.** Contact addresses and identity come
  from the environment with neutral `example.com` fallbacks. The pre-commit hook
  (`scripts/githooks/pre-commit`, enabled with
  `git config core.hooksPath scripts/githooks`) blocks three things in staged
  content: forbidden filenames, credential shapes, and personal data. Its
  personal-data check names nobody — it flags any address outside the
  RFC 2606/6761 example domains, derives a denylist from `git config
  user.email`/`user.name` at run time, and accepts extra values via
  `PERSONAL_DENYLIST`. Prove it still works with
  `sh scripts/githooks/test-guard.sh`.
- **Secrets never enter the repo.** `.env`, `.env.local` and `scratch.md` are
  gitignored *and* blocked by filename. `scratch.md` lives **outside** the repo
  entirely.
- **Review, then push.** Commit locally at each step; push only on explicit
  approval.
- **Fixtures, not real data, in tests.** Example addresses use `example.com` or
  a `.test` domain.
- A guard that cries wolf gets bypassed, so every pattern here is tuned against
  the real tree and has an allow-case in the test suite proving it does not fire
  on ordinary code.
</content>
