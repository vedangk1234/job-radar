# job-radar

A personal, India-scoped job-alert crawler. It watches the ATS boards of a
hand-picked list of companies, stores every posting it finds without judging any
of them, and emails a digest containing only roles that are *genuinely* new and
match a tight filter — with a private dashboard to manage the watch list, edit
the filter, and see what the filter dropped. "New" means *we* had never seen the
posting before, not that a vendor's date field says so; that distinction is the
reason the inbox stays worth opening.

This file is a **living record of what the project is and how it works**. It
describes the code as it stands, not a specification the code has to obey — when
the code changes, this changes with it. Read it to get oriented; do not treat it
as a constraint on a future design.

---

## Architecture (the three-stage spine)

Every stage is deliberately allowed to know less than the next one.

```
CRAWL                  STORE / DIFF                  NOTIFY
fetch every job        observation-based             filter tightly
no judgment      ->    never trusts source dates  -> email exactly once
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
3. **Notify** — apply the filter, email what survives, and record that it was
   sent so it is never sent twice.

The slogan is **store loosely, notify tightly**. `jobs` keeps every posting in
every city forever; `preferences` decides what reaches an inbox, and can be
changed at any time without losing the history to re-decide against.

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
| `city` | computed label: `blr` \| `pune` \| `hyd` \| `india-unclear` \| `other` |
| `source_posted_at` | advisory only, **never** trusted for "new" |
| `first_seen_at` | the authoritative "new" timestamp — our own observation |
| `last_seen_at` | most recent fetch that contained it |
| `miss_count` | consecutive fetches that did *not* contain it |
| `status` | `open` \| `closed` |
| `notified` | it has been emailed (or was stored during a silent seed) |
| `min_experience` | years the description demands as a minimum. **NULL = unknown, and unknown means keep** |
| `is_pm_role` | the description frames the role as product/people management |
| `desc_checked` | the description has been read once; never read twice |
| `desc_fetch_failed` | we looked and could not read it — retried, occasionally |
| `desc_checked_at` | when we last looked. NULL = never |
| `applied` | **dashboard-owned.** I applied to this. The crawler never writes it |
| `applied_at` | when the box was first ticked. NULL whenever `applied` is FALSE |

The five description-derived columns are the **only** trace a job description
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
Written by `scripts/run_discovery.py`: `job_count`, `last_posting_at`,
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
receipt per email, so a job is never emailed twice.

### `preferences` — the filter, as data rather than code
Columns: `cities`, `keywords`, `exclusions` (all `TEXT[]`), `max_level` (`INT`,
default 2), `updated_at`, and `user_id` (`UUID`, FK to `public.users`).

It lives in the database and not in a Python constant precisely so the dashboard
can `UPDATE` it and the very next digest obeys — no redeploy, no commit.
`notify/match.py` carries `DEFAULT_*` constants that mirror the seed for tests
and un-migrated databases; the database is the runtime source of truth and the
constants are never an override.

**The row has an owner, not a fixed address.** It used to be found at `id = 1` —
`id INT PRIMARY KEY DEFAULT 1 CHECK (id = 1)` — and every loader in both
runtimes said so. Phase 1b replaced that with `WHERE user_id = <current user>`;
`preferences_one_per_user_idx` (a *partial unique index*, chosen so named
multi-profiles stay one `DROP INDEX` away) enforces one row per user. The
`CHECK (id = 1)` is still there and still caps the table at one row overall;
that comes down in whichever sub-phase first needs a second row.

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

**The nullable window.** `user_id` is nullable, because `ADD COLUMN … NOT NULL`
would need a default the schema cannot know — the UUID is resolved from the
environment. So applying `db/schema.sql` and running
`scripts/backfill_preferences_user.py` is **one unit, with no crawl or digest
between them**. In that window a user-scoped loader finds nothing, and both
loaders tell that apart from a genuinely absent row (one extra query on the miss
path) and **raise** rather than falling back to the seeded defaults — silently
emailing the results of a filter nobody chose is exactly the failure the crawl
guards exist to prevent.

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
looks new. A seeding run therefore stores everything with `notified = TRUE`, so
nothing is ever emailed for it; real alerting starts on crawl #2. `seeding` is
decided by the caller from the company's stored row count and changes exactly
one thing — that flag.

Nothing is emailed from the crawl loop. Brand-new jobs are simply left
`notified = FALSE`, and that flag *is* the queue the digest reads.

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
is_match  =  city_ok
         AND keyword_ok
         AND NOT excluded            (a word in the title)
         AND NOT level_excluded      (a level notation in the title)
         AND NOT is_pm_role          (a fact from the description)
         AND (min_experience IS NULL OR min_experience <= 2)
```

The first four are the **title stage** (`title_stage_ok`) — free, and run over
every open job. The last two read columns, not text, and the seam is named
explicitly because the enrichment pass needs to know which jobs are worth a
network request: the ones that already pass every title test.

**Every ambiguity resolves toward keeping the job — except the city.**
`min_experience IS NULL` covers three different situations — never read, read
and no minimum stated, and fetch failed — and all three mean *we do not know, so
do not hide it*. Only a confident minimum above 2, or a detected PM role, hides
a posting. Likewise a title stating no level survives: treating silence as
senior would empty the digest.

`city_ok` is the one test with no inclusion bias, and the asymmetry is the
point. A description can be absent and a level can go unstated, but a location
string is always present and always readable — so a label outside
`preferences.cities` is an answer rather than a gap, and a NULL label is a bug
rather than a doubt. `or ""` turns an unlabelled row into the empty string,
which is in no preferences list, so "we never labelled it" can never read as a
pass. The seeded default is three cities exactly; `india-unclear` used to be
there too and is not any more (see *City labelling*). Re-adding it from the
preferences screen restores the old behaviour on the next digest — the filter is
data, which is the whole reason it is.

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

**Preferences are DB-backed and dashboard-editable**: `keywords`, `cities`,
`exclusions`, `max_level`. The API rejects an empty `cities` or `keywords` list,
because either would match no job at all.

### Enrichment

`crawler/enrich.py` runs **during the crawl**, once per cycle, over title-stage
matches only — a few dozen requests rather than one per row on the board. It
fetches each candidate's description exactly once for as long as the posting
exists, reduces it to `min_experience` and `is_pm_role`, writes those plus the
three bookkeeping flags, and discards the text.

It used to run only in the digest path, which reads the `notified = FALSE`
queue — so a description was first read when a job was first considered for an
*email*. The dashboard's Home screen asks a different question (everything open
that fits, emailed or not), so a freshly-discovered job appeared there with
nothing known about its experience requirement and was therefore kept. That is
correct behaviour on incomplete evidence; the fix was the evidence, not the rule.
One crawl cycle is now the worst case for how long a job can be shown with its
experience unknown.

Enrichment must never touch a lifecycle column, and that is a property of the
SQL rather than a promise: the `UPDATE` in `notify/store.py` names only the five
description columns, so it cannot re-open a closed job or clear a notified flag.

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

`fetchers/normalize.city_label` returns `blr` | `pune` | `hyd` |
`india-unclear` | `other`. Precedence: a named target city wins, then a **named
Indian place that is not one of them** (`other`), then a country/state/region
signal that names no city (`india-unclear`), then a confident non-India anchor
(`other`), then vagueness — "Remote", "Multiple Locations" — which resolves to
`india-unclear`. An empty location is `india-unclear`; an unlabelled posting on
an Indian company's board is more likely Indian than not.

Matching is generous on purpose: a missed Bangalore posting is a job never seen,
a false positive is one line of noise. When a string is **ambiguous** the bias
is `india-unclear` rather than `other`, because `india-unclear` stays in the
funnel and `other` drops out of it.

**Ambiguous is not the same as "Indian, elsewhere", and conflating them leaked
jobs.** Every India signal used to share one bucket, so "Lucknow" and "Remote"
got the same label — even though one is a confident statement that the posting
is not in any of the three cities and the other is no statement at all. With
`india-unclear` in `preferences.cities`, an InMobi "SDE 2" in Lucknow read as a
match, and so did Noida, Mohali and Gurgaon rows: 14 of 61. A string that names
an Indian place we did not ask for is now `other`, for the same reason "London"
is — the uncertainty the inclusion bias protects is simply not present.

The three home states are the deliberate exception and stay `india-unclear`:
Karnataka contains Bengaluru, Maharashtra contains Pune, Telangana contains
Hyderabad, so a posting labelled with only one of them may well be in a city we
want. "Uttar Pradesh" cannot be, and is `other`.

`other` therefore now answers "not one of my cities" rather than "not India",
and one call site needed the older question kept: the Workday **unfiltered
fallback**, which trims an unscoped tenant's board before storing it. It asks
`normalize.india_funnel` instead — target city, Indian elsewhere, or vague — so
it goes on storing the Mumbai rows it always did. Had it been left reading
`city_label(...) != "other"` it would have silently begun dropping them, which
would be the crawler filtering jobs: exactly what *store loosely* forbids.

Beyond full city names, the markers include:

- **Office/airport codes** — `blr`, `hyd`, `pnq`, and `ind pun`/`in pun`. One
  Workday tenant writes every Bangalore posting as "IND-BLR-<building>", which
  matched nothing and labelled `other`: **135 real Bengaluru jobs were being
  discarded by a string format.** A three-letter token is safe here because
  `norm()` has already turned every hyphen, comma and slash into a space and
  `_hit` anchors on word boundaries, so `blr` matches inside "IND-BLR-Divyasree"
  and cannot match inside a longer word. `ind pun` is anchored as a whole phrase
  so it matches "IND-PUN-Kharadi" and **not** "IND-Punjab-Mohali".
- **One misspelling** — `banglore`, which one Greenhouse board writes on every
  posting from that office (3 rows). It is the only near-miss in the entire
  table: a fuzzy pass over every 4+ letter token in all 1197 stored rows found
  nothing else. Hyderabad and Pune are spelled correctly on every board here, so
  neither gets a misspelling marker — one that matches nothing is
  false-positive surface bought with no recall.

Deliberately **rejected**, each after being checked against the data: `beng`
(occurs only inside "Bengaluru", and standing alone is likelier to be Bengal);
bare `pun` (an English word, and PUN is not Pune's IATA code — PNQ is);
"bangalore urban" (already matches, since `_hit` anchors words rather than whole
strings); and `bengalore`/`bangalor`/`banaglore` (plausible, but absent from the
database and from fixtures — the rule for adding a marker is that a real posting
was lost to it). The nearest trap in the data is real: "THA Bangkok One
Bangkok", 3 rows. Anchoring whole non-word tokens is what makes it impossible
for a "bang" prefix rule to relabel three Thai jobs as Bangalore.

`scripts/survey_locations.py` is what answers "which spellings deserve a
marker?" from the data instead of from English, and `scripts/relabel_cities.py`
recomputes `jobs.city` for stored rows. The relabel pass hashes every column
*except* `city` before and after and asserts they are identical, so "the UPDATE
only touches city" is evidence rather than an argument.

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
was hand-typed. `scripts/run_discovery.py` is the machine that grows it.

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
  with `notified = TRUE` — the same silent seed every other company got. That is
  the entire reason a run that tracks forty boards does not produce forty
  boards' worth of email, and `tests/test_discovery.py` asserts it twice: once
  against the statement text, once by counting rows after a track.
- **Never `DO UPDATE` on `companies`.** The upsert is `ON CONFLICT DO NOTHING`.
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

- **Home (`/`)** — per-company collapsible rows showing **matches only**, with a
  "for you" count per company and, once there is one, an "N applied" count
  beside it. `notified` is deliberately *not* part of this filter: Home answers
  "what fits me right now", not "what has not been emailed yet" — a job sent in
  this morning's digest is still worth applying to this afternoon. It is shown
  as a badge instead. Every job row carries an **Applied** checkbox.
- **Jobs (`/jobs`)** — literally the same component with one prop different
  (`mode="all"`), showing every open posting unfiltered. This is the screen that
  answers "did the crawler miss it, or did the filter drop it?". In this mode
  each job additionally shows its `status`, whether it is notified, and a
  SUGGESTED badge if it also passes the filter. There is no Applied checkbox
  here: this screen is the raw reference, and the tick belongs on the list of
  jobs worth applying to.
- **Add (`/add`)** — resolve a company **by name** (guess the board token from
  the name, trying mechanical variants against greenhouse → lever → ashby, first
  non-empty board wins) or **by URL** (including Workday, parsed for
  host/tenant/site).
- **Preferences (`/preferences`)** — edits the single `preferences` row. `PUT`
  rather than `PATCH`, because the editor sends the full lists it is showing.

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

### Status badges — derived, never stored

One badge per job row, computed at render from data the row already carries:

| Badge | Screen | Rule |
|---|---|---|
| `NEW` | Home | is a match **and** `notified = FALSE` — queued, not yet emailed |
| `NOTIFIED` | Home | is a match **and** `notified = TRUE` — already went out |
| `SUGGESTED` | Jobs | passes the same shared filter, on a list that does not apply it |

A non-matching row on Jobs gets no badge, which is the point — that screen is
several hundred postings including the sales roles, and SUGGESTED is what makes
the few that fit findable in it. Showing NEW/NOTIFIED there, or SUGGESTED on
Home, would put the same badge on every row and therefore say nothing.

There is **no column** for any of this and there must not be. A badge is a
reading of the match rules plus `notified`, and the match rules read the
`preferences` row, which is editable from another tab — so a stored badge would
be a cached answer to a question whose inputs change under it, wrong within
seconds and wrong silently. Deriving it costs one boolean per row and cannot go
stale. `dashboard/src/lib/badges.ts` states the rule; the `matches` half comes
from `lib/match.ts`, the same parity-checked port the "for you" count uses.

NEW is not speculative. It was originally verified by temporarily flipping one
live match to `notified = FALSE`, confirming the badge, and restoring the row —
because at that point the table had been silently seeded and nothing else had
ever been queued. Since Step 6 it is simply the ordinary state: the hourly crawl
queues genuinely-new postings at `notified = FALSE`, and they read NEW on Home
until that hour's digest sends and flips them.

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

**Writes the dashboard is allowed to make**, and nothing else:
`companies.tracked`, `companies.category`, the single `preferences` row,
inserting a company, and `jobs.applied` / `jobs.applied_at`. Everything else
about a job stays read-only — the crawler owns the lifecycle, and a dashboard
able to edit `notified` or `status` could silently un-send or re-send a digest.
The applied pair is not a hole in that rule but the other side of it: those two
columns record something no crawl can observe, so nothing but the dashboard
could write them. `lib/jobs.setApplied` is the one statement in the app that
writes to `jobs`, and it names those two columns and no others; the route that
*lists* jobs is deliberately a different file, so "the listing route cannot
change a job" stays true by inspection. The dashboard still has no cron, sends
no email, and never fetches a job description.

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
| `npm run match-parity` | `lib/match.ts` against `notify/match.py`, over every open row in the live database |

Run `match-parity` after touching either matcher; its total is directly
comparable to `python scripts/preview_digest.py --dry-run --ignore-notified`.

A third check answers a different question. `npm run ui-check` needs a **running**
dev server: it fetches real rows from the real API over HTTP with a minted
session, renders the shipped `JobLine` component with them, and asserts the
checkbox and the badges are actually in the markup. It exists because the Applied
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
| `MY_EMAIL` | `notify/digest.py`, `fetchers/http.py`, `dashboard/src/lib/ats.ts`, **`db/users.py`**, **`dashboard/src/lib/currentUser.ts`** | digest recipient; the contact address in the outbound User-Agent; and, since Phase 1b, **the identity the current user is resolved from in both runtimes**. Falls back to `unknown@example.com` in the two User-Agent paths only. The digest requires it, and so does anything that loads preferences — including **the dashboard**, where it was previously optional. Must now be set on Vercel |
| `GOOGLE_CLIENT_ID` | `dashboard/src/auth.ts` | |
| `GOOGLE_CLIENT_SECRET` | `dashboard/src/auth.ts` | |
| `AUTH_SECRET` | Auth.js | signs the session cookie; changing it logs you out everywhere |
| `ALLOWED_EMAIL` | `dashboard/src/lib/env.ts` | the one account allowed to sign in. **No default** — `required()` throws when unset, and every fallback path authorises nobody |
| `AUTH_URL` | Auth.js | *optional* — only if Auth.js cannot infer its own URL. Local dev and Vercel both infer it correctly |
| `PERSONAL_DENYLIST` | `scripts/githooks/pre-commit` | *optional* — extra values the personal-data guard should block |
| `GITHUB_TOKEN` | `fetchers/discovery.py` | *optional* — unlocks GitHub **code** search, which is 401 unauthenticated. Without it the sweep falls back to repository search + README mining and works fine, just narrower |

Because the sandbox sender can only deliver to the address that owns the API
key, `notify/digest.py` asserts locally that the recipient equals `MY_EMAIL`
before making any network call — so a mistyped recipient fails with a message
naming the actual constraint rather than as a 403 that reads like an auth error.

---

## Automation — the hourly cron

`.github/workflows/crawl.yml` is the whole of it. It orchestrates the two
scripts that were already proven by hand and adds no logic of its own:

```
every hour (cron '0 * * * *', UTC)   or   Actions -> crawl -> Run workflow
  └─ checkout -> setup-python 3.12 -> pip install -r requirements.txt
     └─ verify secrets -> python scripts/crawl.py -> python scripts/send_digest.py --tag cron
```

The repo is public, so Actions minutes are free and unlimited.

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
- **`timeout-minutes: 20`.** A full crawl of 14 boards takes ~2 minutes. The
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
| `MY_EMAIL` | `.env` | must be the Resend account address |

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
| `Send the digest` | Resend rejected the send; the message names the HTTP status |

A red `Send the digest` **loses nothing**. A failed send is a no-write, so the
matched jobs stay `notified = FALSE` and the next hourly run picks them up. The
same is true of a run that never started: the queue is a database column, not an
in-memory list, so a skipped hour is deferred rather than dropped. GitHub's
scheduler is best-effort and does run late under load — the cron is a floor on
the interval, not a promise.

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

**Pending**

- Work the `discovered_companies` "try Workday" queue by hand with
  `scripts/add_workday.py` — the sweep cannot resolve a Workday site.

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
