-- job-radar schema. Idempotent: safe to re-run against an existing database.

-- companies: the live working set
CREATE TABLE IF NOT EXISTS companies (
  id            SERIAL PRIMARY KEY,
  name          TEXT NOT NULL,
  ats           TEXT NOT NULL,           -- greenhouse | lever | ashby | smartrecruiters | workable | recruitee | workday | custom_jsonld | custom_sitemap
  token         TEXT NOT NULL,           -- board token; for workday, the tenant
  config        JSONB NOT NULL DEFAULT '{}'::jsonb,  -- ATS extras: workday {host, site}; custom {url}
  category      TEXT,                    -- product | gcc | services | staffing | unknown (hand-tagged once)
  tracked       BOOLEAN NOT NULL DEFAULT FALSE,      -- true = 30-min tier, false = daily
  india_scoped  BOOLEAN NOT NULL DEFAULT FALSE,      -- data is partial (India filter applied at source)
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (ats, token)
);

-- jobs: metadata only, NEVER job descriptions (keeps us on the free tier)
--
-- The five description-derived columns at the bottom are the ONLY trace a job
-- description ever leaves in this database. The second-stage filter reads each
-- candidate's full description over the network, reduces it to two facts and
-- three bookkeeping flags, and throws the text away. No description column
-- exists, on purpose: two integers per job cost nothing, and 10 KB of HTML per
-- job times a growing board is how a free tier turns into a bill.
CREATE TABLE IF NOT EXISTS jobs (
  canonical_id     TEXT PRIMARY KEY,
  company_id       INT NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  title            TEXT NOT NULL,
  location         TEXT,                 -- raw string, kept verbatim
  city             TEXT,                 -- computed label: blr | pune | hyd | india-unclear | other
  url              TEXT NOT NULL,
  source_posted_at TIMESTAMPTZ,          -- advisory only, never trusted for "new"
  first_seen_at    TIMESTAMPTZ NOT NULL, -- authoritative "new" timestamp (our own observation)
  last_seen_at     TIMESTAMPTZ NOT NULL,
  miss_count       INT NOT NULL DEFAULT 0,
  status           TEXT NOT NULL DEFAULT 'open',   -- open | closed
  notified         BOOLEAN NOT NULL DEFAULT FALSE,
  min_experience    INT,                            -- years the description demands as a MINIMUM; NULL = unknown
  is_pm_role        BOOLEAN NOT NULL DEFAULT FALSE, -- the description frames the role as product/people management
  desc_checked      BOOLEAN NOT NULL DEFAULT FALSE, -- the description has been read once; never read twice
  desc_fetch_failed BOOLEAN NOT NULL DEFAULT FALSE, -- we looked and could not read it; retried occasionally
  desc_checked_at   TIMESTAMPTZ,                    -- when we last looked; NULL = never (or before this column)
  applied           BOOLEAN NOT NULL DEFAULT FALSE, -- DASHBOARD-OWNED: I have applied to this. The crawler never writes it.
  applied_at        TIMESTAMPTZ                     -- when the checkbox was ticked; NULL whenever applied is FALSE
);
CREATE INDEX IF NOT EXISTS jobs_company_status_idx ON jobs (company_id, status);

-- The same five columns for a database created before they existed. Additive
-- and idempotent, exactly like the preferences.max_level migration below: the
-- DEFAULTs backfill every existing row (FALSE / FALSE), and min_experience is
-- deliberately nullable because "we have not looked" and "no minimum stated"
-- are the same answer and both mean KEEP. See notify/match.is_match.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS min_experience INT;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS is_pm_role   BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS desc_checked BOOLEAN NOT NULL DEFAULT FALSE;

-- desc_fetch_failed separates the two things desc_checked used to conflate.
--
-- "We read the description and it stated no minimum" and "we asked and got
-- nothing back" both leave min_experience NULL, and both mean KEEP — but only
-- the second one is worth ever asking about again. Without this flag the only
-- way to retry a transient failure is to re-read every description forever, so
-- the previous code never retried at all and a board with no description route
-- was silently marked "checked, nothing found" for good.
--
-- desc_checked_at is what makes the retry occasional rather than constant: a
-- failed row is re-asked once a day, not once a crawl. See crawler/enrich.py.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS desc_fetch_failed BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS desc_checked_at   TIMESTAMPTZ;

-- applied / applied_at — the ONLY two columns on this table the DASHBOARD owns.
--
-- Every other column here is the crawler's: it observed the posting, so it is
-- the only thing entitled to say when the posting appeared, whether it is still
-- open, or whether it has been emailed. These two are the opposite — they record
-- something only a person can know, which no fetch can ever discover and no
-- crawl may ever overwrite.
--
-- That ownership is enforced by the SQL rather than by a rule anyone has to
-- remember. crawler/store.UPSERT_SEEN names neither column in its INSERT list
-- nor in its ON CONFLICT DO UPDATE clause, so a re-sighting cannot clear the
-- tick; notify/store.SAVE_DESCRIPTION_FACTS_SQL names neither either, so
-- enrichment cannot. tests/test_applied_column.py asserts both, against the
-- production statements, so the guarantee fails loudly rather than silently.
--
-- applied_at is nullable and is cleared whenever applied goes back to FALSE, so
-- the pair can never say "not applied, on the 3rd of August". The dashboard's
-- PATCH route writes both in one statement for exactly that reason.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS applied    BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS applied_at TIMESTAMPTZ;

-- jobs.seeded — "this row was stored during a company's first-ever crawl."
--
-- THE SEED MARKER, TAKEN OUT OF notified. Until Phase 1e the crawl recorded a
-- silent seed by writing notified = TRUE across the whole back catalogue, and
-- that one column was carrying two unrelated facts:
--
--     "this row is back catalogue"   a property of the CRAWL. User-blind,
--                                    permanent, and true for everybody forever.
--     "somebody was emailed this"    a property of a PERSON — which is exactly
--                                    why 1d moved it into user_job_notifications.
--
-- Conflating them is why the 1d queue had to be a CONJUNCTION: read the per-user
-- table alone and the seed meaning has nowhere left to live, so a freshly-seeded
-- back catalogue walks straight into the next digest. One discovery sweep once
-- seeded 6,496 rows in an afternoon.
--
-- So this column takes the first fact and keeps it where the crawl can write it
-- without learning anything about users. crawler/store.mark_new writes it in the
-- same statement it writes notified, from the same `seeding` flag decided by
-- scripts/crawl.py from the company's stored row count. The crawl path stays
-- user-blind — tests/test_user_companies.py and tests/test_user_notifications.py
-- both still assert that absence three ways, and a column about a crawl event is
-- not a step towards it.
--
-- READ BY THE QUEUE, in the place notified = FALSE used to sit. See
-- notify/store.CANDIDATES_SQL: a seeded row is never emailed to anyone, ever,
-- which is precisely what "silent seed" has always meant. What that bought is
-- that 1g's drop of `notified` was a genuine no-op rather than a behaviour
-- change with an inbox-shaped failure mode — the queue had already been reading
-- this column for a phase by the time the old one went.
--
-- NOT NULL DEFAULT FALSE, so the ALTER backfills every existing row to "not
-- seeded" — which is the SAFE direction and deliberately not the accurate one.
-- FALSE means "eligible for someone's queue", and every historical row that
-- really was seeded already carries a user_job_notifications row naming the one
-- existing user (the 1d backfill), so nothing enters a queue it was not already
-- in. scripts/backfill_seeded_flag.py is what then makes the column HONEST about
-- history, and it gates on exactly that: it refuses to stamp a row that is not
-- already covered for every user who tracks its company, so a mistake in the
-- reconstruction cannot remove anything from anybody's digest.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS seeded BOOLEAN NOT NULL DEFAULT FALSE;

-- jobs.notified — DROPPED HERE, PHASE 1g.
--
-- THE FIRST DROP COLUMN IN THIS FILE, AND IT FOLLOWS AN EXISTING SHAPE. The
-- `DROP CONSTRAINT IF EXISTS preferences_id_check` near the bottom of the
-- preferences block is the precedent: the CREATE TABLE above keeps the column in
-- its list, describing the table as it was built, and the removal is stated once,
-- here, where there is room to say why. A fresh database therefore creates the
-- column and drops it a few statements later — which costs nothing, keeps this
-- file re-runnable in one pass, and leaves the reason for the removal in exactly
-- one place instead of split between an absence and a note.
--
-- IF EXISTS, so re-running this file after the drop is a no-op like every other
-- statement here.
--
-- WHY THE COLUMN IS DEAD. It carried two different facts under one name:
--
--     "this row is back catalogue"   a property of the CRAWL. Now jobs.seeded,
--                                    written by crawler/store.mark_new from the
--                                    seeding flag, read by the digest queue.
--     "somebody was emailed this"    a property of a PERSON. Now a row in
--                                    user_job_notifications, one per (user, job).
--
-- Conflating them is why one person's digest silenced a job for everybody else:
-- a boolean on a shared row cannot answer a question about an individual. 1d gave
-- the person half an owner, 1e moved the crawl half into `seeded` and switched
-- the queue onto both, 1f moved the dashboard's NEW/NOTIFIED badge onto the
-- per-user table, and by then nothing read this column at all.
--
-- WHY IT SURVIVED THREE SUB-PHASES AFTER ITS LAST READER. It was the rollback
-- net: record_sent kept writing it in the same transaction as the per-user row,
-- so reverting 1d could not have re-sent anything that went out during the 1d
-- window, and scripts/backfill_user_notifications.py had an accurate source to
-- re-run against. That net is what made each step reversible one at a time.
--
-- WHY IT DID NOT SURVIVE 1g. A net for a rollback nobody can still perform is
-- not a net. Once 1e was proven in production the earlier phases stopped being
-- revertible in any case, and a column written by one statement and read by none
-- is worse than absent: the next person to find it reasonably assumes something
-- depends on it, and the first edit made on that assumption is a silent one.
--
-- WHAT HAD TO BE TRUE FIRST, and all three were, before this ran:
--   * no page render, query or badge read it — tests/test_dashboard_notified.py
--     asserts that textually over every file in dashboard/, with no exemptions;
--   * the code that stops using it was DEPLOYED, not merely written. The cron and
--     the dashboard share this database, so old code plus a dropped column is an
--     outage, not a safe no-op. Expand, then contract;
--   * every job the boolean called notified was already recorded per-user, and
--     every silently-seeded row already carried jobs.seeded. The two backfills
--     that established that read this column, so they ran before this line
--     existed and were deleted alongside it — see the git history for 1d and 1e
--     if a future migration ever needs to see how they were gated.
ALTER TABLE jobs DROP COLUMN IF EXISTS notified;

-- One-time re-open of the rows the OLD enrichment marked checked without ever
-- reading anything. Before crawler/enrich.py existed, enrichment ran only in
-- the digest path, and notify/description.fetch_description had no route for
-- Workday at all — so every Workday candidate was fetched as None, learned
-- nothing, and was still stamped desc_checked = TRUE and never asked again.
-- That is exactly the hole this migration accompanies: an Adobe posting
-- demanding 4-6 years sat on Home as "experience unknown, therefore keep".
--
-- The predicate is "checked, but the check produced no fact", which is the only
-- state where a re-read can change an answer. `desc_checked_at IS NULL` is what
-- makes it run exactly once: every row the new pass touches gets a timestamp,
-- so re-applying this schema afterwards cannot undo a fresh check.
UPDATE jobs
   SET desc_checked = FALSE
 WHERE desc_checked = TRUE
   AND desc_checked_at IS NULL
   AND min_experience IS NULL
   AND is_pm_role = FALSE;

-- discovered_companies: staging area for the (later) discovery script; promoted by hand into companies
CREATE TABLE IF NOT EXISTS discovered_companies (
  id                  SERIAL PRIMARY KEY,
  name                TEXT,
  ats                 TEXT NOT NULL,
  token               TEXT NOT NULL,
  config              JSONB NOT NULL DEFAULT '{}'::jsonb,
  job_count           INT,
  last_posting_at     TIMESTAMPTZ,
  city_hits           TEXT[],           -- which of the three cities appeared
  looks_like_services BOOLEAN NOT NULL DEFAULT FALSE,  -- name-regex heuristic
  source              TEXT,             -- commoncrawl | github | domain_crawl
  discovered_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  promoted            BOOLEAN NOT NULL DEFAULT FALSE,
  UNIQUE (ats, token)
);

-- notifications: sent-log so a job is never emailed twice
CREATE TABLE IF NOT EXISTS notifications (
  id           SERIAL PRIMARY KEY,
  canonical_id TEXT NOT NULL REFERENCES jobs(canonical_id) ON DELETE CASCADE,
  sent_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  digest       TEXT              -- e.g. '2026-07-25-am'
);

-- preferences: the notify-tightly filter, as data rather than as code.
--
-- The jobs table stores everything, every city, unfiltered. This single row is
-- the only thing that decides what reaches an inbox, which is why it lives in
-- the database and not in a Python constant: the Step 5 dashboard edits it
-- with an UPDATE, and the very next digest obeys it. No redeploy, no commit.
--
-- Exactly one row, forever — as this table was FIRST created. The CHECK plus
-- the fixed default enforced that: every INSERT landed on id=1 and collided with
-- the row already there, so "the preferences" was never an ambiguous question.
--
-- That is no longer true and the statement is left standing only because
-- CREATE TABLE IF NOT EXISTS never re-runs against a live database. Phase 1b
-- gave the row an OWNER (user_id, below) and Phase 1e took the CHECK back off,
-- because a fan-out with one filter is not a fan-out. Read on to the
-- DROP CONSTRAINT near the bottom of this block; the rule now is one row per
-- USER, enforced by preferences_one_per_user_idx.
CREATE TABLE IF NOT EXISTS preferences (
  id          INT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  cities      TEXT[] NOT NULL DEFAULT '{}',  -- city labels to include; see fetchers/normalize.city_label
  keywords    TEXT[] NOT NULL DEFAULT '{}',  -- title must match at least one
  exclusions  TEXT[] NOT NULL DEFAULT '{}',  -- title must match none
  max_level   INT NOT NULL DEFAULT 2,        -- drop titles stating a level above this; see notify.match.detect_level
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- max_level for a database created before the column existed. The DEFAULT is
-- what backfills the row that is already there: Postgres writes 2 into it as
-- part of the ADD COLUMN, so the existing preferences row comes out of this at
-- max_level = 2 without a separate UPDATE — and re-running the schema later
-- cannot reset a value the dashboard has since edited.
ALTER TABLE preferences ADD COLUMN IF NOT EXISTS max_level INT NOT NULL DEFAULT 2;

-- Seed the row if the table is empty. ON CONFLICT DO NOTHING is what makes
-- this safe to re-run: once the dashboard has edited the row, re-applying the
-- schema must not quietly reset the filter to these defaults.
INSERT INTO preferences (id, cities, keywords, exclusions, max_level, updated_at)
VALUES (
  1,
  -- Three cities, strictly. 'india-unclear' was here and is not any more: see
  -- notify/match.DEFAULT_CITIES for why, and add it back from the preferences
  -- screen (not here) if the noise turns out to be worth it.
  ARRAY['blr', 'pune', 'hyd'],
  ARRAY['software engineer', 'software developer', 'software development engineer',
        'sde', 'associate software engineer', 'member of technical staff', 'mts',
        'application developer', 'programmer', 'forward deployed engineer',
        'backend', 'back end', 'node', 'nodejs', 'express', 'fastapi',
        'api', 'rest', 'microservices', 'distributed',
        'full stack', 'fullstack', 'frontend', 'front end',
        'react', 'react native', 'nextjs', 'next.js',
        'python', 'javascript', 'typescript',
        'platform', 'infrastructure', 'infra', 'devops', 'cloud engineer',
        'machine learning', 'ml engineer', 'ai engineer', 'mlops',
        'deep learning', 'computer vision', 'llm'],
  ARRAY['intern', 'senior', 'staff', 'principal', 'lead', 'manager',
        'director', 'head', 'architect', 'sales', 'recruiter', 'support',
        'qa', 'analyst'],
  2,
  now()
)
ON CONFLICT (id) DO NOTHING;

-- New keywords, for the row the INSERT above deliberately declined to touch.
-- Additive and guarded by a membership test, so it is idempotent and cannot
-- undo an edit made in the dashboard — the one thing ON CONFLICT DO NOTHING is
-- protecting. Every keyword added here must also be added to DEFAULT_KEYWORDS
-- in notify/match.py; the constant is a mirror of this seed, never an override.
UPDATE preferences
   SET keywords = keywords || ARRAY['forward deployed engineer'],
       updated_at = now()
 WHERE id = 1
   AND NOT ('forward deployed engineer' = ANY (keywords));

-- users: the account this whole install belongs to.
--
-- Until now the single user was implicit — one preferences row, one ALLOWED_EMAIL,
-- one MY_EMAIL digest recipient, and nothing in the database naming any of them.
-- This table is that user made explicit, and it is the anchor the rest of Phase 1
-- hangs off. Nothing reads it yet: no crawler path, no dashboard route, no foreign
-- key. It is deliberately inert so that adding it can break nothing.
--
-- id is a UUID rather than the SERIAL the rest of this schema uses, for one
-- forward-looking reason: Phase 2 adds magic-link auth, and both Supabase Auth
-- (auth.users.id) and the Auth.js database adapters key users by uuid. A SERIAL
-- here would mean carrying a separate provider-id column to join on. gen_random_uuid()
-- is built into Postgres 13+, so this needs no extension on Supabase.
--
-- SCHEMA-QUALIFIED, unlike every other table in this file, and it has to be:
-- Supabase ships its own auth.users, so `users` is the one ambiguous table name
-- on this database. A bare CREATE TABLE IF NOT EXISTS resolves against the
-- connection's search_path, and db/connection.py sets none — so the target
-- schema is a server-side default this file does not own. The failure that
-- guards against is silent rather than loud: with auth ahead of public on the
-- path, `IF NOT EXISTS` would find auth.users already there, skip, report
-- success, and leave public.users uncreated. Naming the schema makes the target
-- a property of the statement instead of of the session.
CREATE TABLE IF NOT EXISTS public.users (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  email       TEXT NOT NULL UNIQUE,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  plan        TEXT NOT NULL DEFAULT 'free',   -- free | (future tiers); no CHECK, matching companies.category
  is_active   BOOLEAN NOT NULL DEFAULT TRUE
);

-- Email identity is case-insensitive in practice but TEXT UNIQUE is not, so
-- 'Me@example.com' and 'me@example.com' would be two accounts — and with
-- magic-link auth in Phase 2 that is two accounts a person cannot tell apart,
-- silently. The plain
-- UNIQUE above stays (it is what ON CONFLICT (email) targets); this index is what
-- actually makes the identity one-per-person.
CREATE UNIQUE INDEX IF NOT EXISTS users_email_lower_idx ON public.users (lower(email));

-- No seed row here, deliberately, and this is the one place the seed cannot
-- follow the preferences pattern above. The only user is a real person with a
-- real address, and "no hardcoded personal data, anywhere" is enforced rather
-- than requested: scripts/githooks/pre-commit blocks any address outside the
-- RFC 2606/6761 example domains, so this INSERT could not be committed even if
-- it were written. The seed lives in scripts/seed_user.py, which reads MY_EMAIL
-- from the environment exactly as notify/digest.py does.

-- preferences.user_id — the filter stops being "the" filter and becomes yours.
--
-- Until now "the preferences" was a row at a fixed address: id = 1, pinned by a
-- CHECK, and every loader in both runtimes said WHERE id = 1. That was correct
-- while the install had one user and nothing in the database naming them. This
-- column is what replaces the fixed address with an owner, and it is the first
-- thing to actually read public.users — the table added above was deliberately
-- inert, and this is what wires it in.
--
-- FK DIRECTION: preferences.user_id -> public.users(id), the child on
-- preferences. A filter is a property of a person; a person is not a property
-- of a filter. This is also the direction that lets one user grow several
-- preference rows later (see the index below); the reverse could not express
-- that at all.
--
-- ON DELETE CASCADE, matching jobs.company_id above. A preferences row that
-- outlives its owner is a filter deciding what reaches nobody's inbox — not
-- data worth keeping, and precisely the row a later "the preferences for this
-- user" lookup would trip over. RESTRICT was the alternative and would make
-- deleting an account a two-step chore whose only benefit is preserving a row
-- nothing can read.
--
-- NULLABLE, deliberately, and this is the part worth reading twice. ADD COLUMN
-- ... NOT NULL needs a DEFAULT, and the only correct default is a UUID resolved
-- from MY_EMAIL at runtime — a value no statement in this file can know, and
-- one that could not be written here anyway without committing a real address.
-- So the column arrives nullable, scripts/backfill_preferences_user.py fills
-- it, and tightening to NOT NULL waits for Phase 2, when auth guarantees a user
-- for every write. Nullable is also what makes this ALTER safe to re-run.
--
-- THE WINDOW THIS OPENS, AND WHAT CLOSES IT: between this ALTER and that
-- backfill the row exists but is unowned, and a user-scoped loader finds
-- nothing. Both loaders detect that state explicitly and refuse rather than
-- falling back to defaults — see notify/store.load_preferences and
-- dashboard/src/lib/preferences.ts, which distinguish "this user has no row"
-- from "the migration is half-applied". Apply this file and run the backfill as
-- one unit, with no crawl or digest in between.
ALTER TABLE preferences ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES public.users(id) ON DELETE CASCADE;

-- One preferences row per user — for now, and phrased so that "for now" is one
-- line to undo.
--
-- A UNIQUE INDEX rather than ALTER TABLE ... ADD CONSTRAINT, for three reasons
-- and the first is decisive: ADD CONSTRAINT has no IF NOT EXISTS in Postgres,
-- so writing it idempotently needs a DO $$ ... $$ block, and this file is
-- re-runnable plain statements from its first line onwards. Second, an index is
-- removed by appending one DROP INDEX IF EXISTS, which keeps that removal
-- append-only too; a table constraint is part of the table's definition and is
-- the kind of thing a later foreign key would target, turning its removal into
-- a negotiation. Third, the partial predicate says out loud that the rule is
-- about OWNED rows, and keeps the pre-backfill state legal.
--
-- What this deliberately does NOT wall off: named multi-profiles. Nothing here
-- says one-row-per-user except this index — the column is a plain nullable
-- UUID and the foreign key is many-to-one by construction. Adding profiles is
-- then: add a name column, DROP INDEX preferences_one_per_user_idx, and add
-- UNIQUE (user_id, name). No table rewrite and no data migration.
--
-- One wall that IS still standing and is not this index's doing: the
-- CHECK (id = 1) on the table above still caps the whole table at one row. That
-- comes down in whichever sub-phase first needs a second preferences row, along
-- with giving id a real sequence. Left alone here on purpose — no consumer
-- today, and a PK reshape is not a change to make speculatively.
CREATE UNIQUE INDEX IF NOT EXISTS preferences_one_per_user_idx
    ON preferences (user_id) WHERE user_id IS NOT NULL;

-- THE ONE-ROW CAP COMES DOWN HERE. Phase 1e, and the sub-phase the paragraph
-- above predicted: "whichever sub-phase first needs a second preferences row".
--
-- The fan-out sends each user a digest of their own matches, and "their own
-- matches" is meaningless while the whole table is capped at one filter. Two
-- users with the same filter would not test isolation at all — it is the second
-- FILTER that proves a digest was computed for a person rather than for the
-- install. So the CHECK goes, and with it the fixed DEFAULT 1, which would make
-- every unqualified INSERT collide with the row already there.
--
-- WHAT DOES NOT CHANGE: preferences_one_per_user_idx above still allows exactly
-- one row per user. This lifts the cap on the TABLE, not the rule about a
-- PERSON, and named multi-profiles stay exactly where the index comment left
-- them — one DROP INDEX away, not accidentally opened by this.
--
-- DROP CONSTRAINT IF EXISTS is the one idempotent shape available: Postgres has
-- no ADD CONSTRAINT IF NOT EXISTS (which is why the rule above is an index), but
-- it does have this, so the removal stays a plain re-runnable statement like
-- every other line in this file. preferences_id_check is the name Postgres gives
-- an inline column CHECK: <table>_<column>_check.
ALTER TABLE preferences DROP CONSTRAINT IF EXISTS preferences_id_check;

-- id needs a real sequence now that more than one row may exist. Three
-- statements rather than a type change, because ALTER COLUMN ... TYPE SERIAL is
-- not a thing Postgres accepts — SERIAL is sugar for exactly this trio, and
-- spelling it out is also what makes each part independently idempotent.
--
-- setval to the current maximum rather than to a literal: the existing row is
-- id=1 today, but this file has to be correct against a database that has
-- already grown a few. COALESCE covers the empty table, which is the state a
-- fresh install applies this in.
CREATE SEQUENCE IF NOT EXISTS preferences_id_seq OWNED BY preferences.id;
ALTER TABLE preferences ALTER COLUMN id SET DEFAULT nextval('preferences_id_seq');
SELECT setval('preferences_id_seq', COALESCE((SELECT max(id) FROM preferences), 1));

-- user_companies: which user watches which company. The schema behind
-- "crawl once, fan out" — and it is only the first half of that phrase.
--
-- The catalog stays SHARED and is crawled EXACTLY ONCE for everybody. Two users
-- watching Adobe is one board fetch an hour, not two, and this table is what
-- makes that statement true rather than a coincidence of there being one user.
-- The fan-out — deciding whose digest a new job reaches — reads this table; the
-- crawl does not, and must not.
--
-- THE CRAWL PATH DOES NOT READ THIS TABLE, AND THAT IS THE POINT.
-- scripts/crawl.py selects `FROM companies WHERE tracked = TRUE` and nothing
-- else, exactly as it did before this table existed. A join here would make the
-- crawl conditional on somebody wanting the results, which inverts the design:
-- the whole reason jobs are stored loosely is that a board's history is worth
-- having before anyone asks for it, and a company nobody currently tracks would
-- otherwise go dark and then re-alert its entire back catalogue the day someone
-- adds it. tests/test_user_companies.py asserts the absence three ways — the
-- statement text, the source text of every crawl-path module, and behaviourally.
--
-- FK DIRECTIONS AND TYPES. user_id is UUID because public.users.id is; company_id
-- is INT because companies.id is a plain SERIAL, exactly as jobs.company_id
-- already declares. The two keys on this one table are deliberately different
-- types, which is precisely the thing that invites a mistake — a UUID company_id
-- would fail loudly here, but only because someone wrote the FK; without it, it
-- would be a column that silently never joins.
--
-- BOTH SIDES ON DELETE CASCADE, and for one reason said twice. A tracking row is
-- a statement about a pair; if either half of the pair is gone the row asserts
-- nothing, and it cannot be read back by any query that joins to what it names.
-- Deleting a user must take their tracking rows with them (same argument as
-- preferences.user_id above: a filter, or a watch list, that outlives its owner
-- decides what reaches nobody's inbox). Deleting a company from the catalog must
-- take every user's tracking row for it (same argument as jobs.company_id): the
-- 1e fan-out joins user_companies -> companies -> jobs, and an orphan there either
-- trips the join or is silently dropped by it, which makes the table's row count
-- disagree with the join's for reasons nobody can see. RESTRICT was the
-- alternative on both sides and buys a manual two-step delete whose only prize is
-- keeping rows nothing can read.
--
-- NOT NULL on both, with no nullable window — unlike preferences.user_id, which
-- had to arrive nullable because ADD COLUMN needed a default the schema could not
-- know. Every row here is created by an INSERT that already holds both ids.
--
-- Surrogate id + UNIQUE (user_id, company_id) rather than a composite primary
-- key, matching notifications and discovered_companies. The UNIQUE is what makes
-- "track this company" idempotent — ON CONFLICT DO NOTHING has something real to
-- conflict against — exactly as UNIQUE (ats, token) does for "add a company".
CREATE TABLE IF NOT EXISTS user_companies (
  id          SERIAL PRIMARY KEY,
  user_id     UUID NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
  company_id  INT  NOT NULL REFERENCES companies(id)    ON DELETE CASCADE,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (user_id, company_id)
);

-- The reverse lookup, added with the table rather than discovered later.
-- UNIQUE (user_id, company_id) already answers "which companies does this user
-- watch", which is the question the dashboard and this task's backfill ask. The
-- fan-out asks the other one — "who watches this company" — once per company
-- carrying new jobs, every digest. Without this index that is a sequential scan
-- per company per digest, on the one path where the row count grows with users
-- times companies.
CREATE INDEX IF NOT EXISTS user_companies_company_idx ON user_companies (company_id);

-- No seed row here, for the same reason public.users has none: the only user is
-- a real person resolved from MY_EMAIL at runtime, and this file cannot name
-- them. scripts/backfill_user_companies.py is the seed — it tracks every company
-- in the catalog for that user, so behaviour after this migration is identical
-- to behaviour before it.

-- user_job_notifications: "this person has been emailed about this job."
--
-- The per-user replacement for the global jobs.notified boolean. A ROW EXISTING
-- is the entire decision — this user has been told about this job. NO ROW means
-- not yet. There is no state in between and no column to misread.
--
-- WHY THE BOOLEAN COULD NOT SURVIVE MULTI-USER. jobs.notified said "somebody was
-- emailed about this", which was an adequate paraphrase of "I was emailed about
-- this" while the install had one user and nothing in the database naming them.
-- The moment there are two, the first user's digest silences the job for the
-- second, who never hears about it at all — a job silently deleted from someone's
-- inbox is the expensive failure this project spends its guards on, and it would
-- leave no trace anywhere. This table is that fact given an owner.
--
-- THE JOB SIDE IS canonical_id, NOT job_id, BECAUSE jobs HAS NO id COLUMN. The
-- jobs primary key is canonical_id TEXT — '{ats}:{company_id}:{native_id}', or
-- 'hash:' + a digest for boards that expose no vendor id. The `notifications`
-- table above already spells this key exactly this way, and a second name for
-- the same key on a sibling table is how a join gets written wrong once and
-- reviewed as correct forever. Hence TEXT here, against the UUID on the other
-- side: the two keys on this one table are deliberately different types, each
-- being what the thing it references actually is, exactly as user_companies
-- carries UUID against INT.
--
-- BOTH SIDES ON DELETE CASCADE, and the argument is the same one twice.
-- user_id -> public.users: a receipt that outlives its owner records that nobody
-- was told something, which is not data (the same argument preferences.user_id
-- and user_companies.user_id make). canonical_id -> jobs: the same argument
-- `notifications.canonical_id` directly above makes, plus a sharper one for this
-- table — the 1e fan-out is an ANTI-JOIN of jobs against these rows, and an
-- orphan is a row no join can ever return, so the table's count and the join's
-- count would disagree for reasons nobody can see. RESTRICT on either side buys
-- a manual multi-step delete whose only prize is keeping unreadable rows.
--
-- Surrogate id + UNIQUE (user_id, canonical_id) rather than a composite primary
-- key, matching notifications, discovered_companies and user_companies. The
-- UNIQUE is what makes "notify this user about this job" idempotent — it is what
-- ON CONFLICT DO NOTHING conflicts against — so a double send is a no-op at the
-- database rather than a thing the caller has to remember not to do.
--
-- notified_at IS RECORD-KEEPING AND NOTHING BRANCHES ON IT. It means "when this
-- notification state was recorded", which for the 1d backfill is the migration
-- itself: for the ~7,700 rows the silent seed marked notified we do not know
-- when, or whether, an email ever went out, and `notifications` holds fifteen
-- receipts against them. Back-dating from first_seen_at would put an invented
-- send date in a column, which is the mistake source_posted_at exists to name.
-- So the column follows notifications.sent_at — written, never read to decide —
-- and deliberately NOT desc_checked_at, which DOES branch (the 24-hour retry
-- gate in notify/store.ENRICHMENT_TARGETS_SQL). A future "re-notify after N
-- days" would have to argue with this paragraph first, because the moment
-- anything branches on this value, "when recorded" and "when sent" stop being
-- interchangeable and the backfilled rows start lying.
CREATE TABLE IF NOT EXISTS user_job_notifications (
  id           SERIAL PRIMARY KEY,
  user_id      UUID NOT NULL REFERENCES public.users(id)   ON DELETE CASCADE,
  canonical_id TEXT NOT NULL REFERENCES jobs(canonical_id) ON DELETE CASCADE,
  notified_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (user_id, canonical_id)
);

-- The reverse lookup, added with the table rather than discovered later — the
-- same reasoning as user_companies_company_idx, and the same honesty about which
-- half of it the UNIQUE already covers.
--
-- UNIQUE (user_id, canonical_id) leads with user_id, so it already serves the
-- per-user question ("has THIS user been told about this job") that the digest's
-- NOT EXISTS asks. This index serves the other direction: the planner driving
-- that anti-join from `jobs` outward, which it will, because jobs is the large
-- relation being scanned and these rows are the small one being probed — and the
-- operational question "who has been told about this job", which is the one a
-- person debugging a missing digest actually asks.
CREATE INDEX IF NOT EXISTS user_job_notifications_job_idx
    ON user_job_notifications (canonical_id);

-- HOW jobs.notified WAS RETIRED, over four sub-phases rather than one.
--
-- Phase 1d moved the READ authority to this table and left the boolean in place,
-- still WRITTEN, for three reasons. All three have since expired, which is what
-- made the drop above safe:
--
--   1. It was the rollback net. notify/store.record_sent wrote this table and
--      the boolean in ONE transaction, so reverting 1d could not re-send
--      anything that went out during the 1d window. A rollback net nobody
--      maintains is not a net — so it was maintained until it was removed.
--   2. It was the backfill's only source of truth. The 1d backfill read
--      WHERE notified = TRUE, and a re-run after a rollback needed it accurate.
--   3. The dashboard still read it — lib/badges.homeBadge derived NEW vs
--      NOTIFIED from it, and the dashboard was not user-scoped until 1f. Dual
--      writing is what kept those badges honest without touching any TypeScript
--      in 1d.
--
-- 1e replaced the seed meaning (below), 1f moved the badge onto this table, and
-- 1g removed the write and then the column — in that order, each step leaving
-- the one before it revertible until the next had been proven in production.
--
-- THE OBLIGATION 1d HANDED TO 1e, AND HOW 1e DISCHARGED IT.
-- The 1d digest queue was a CONJUNCTION, not a straight swap:
--
--     AND j.notified = FALSE
--     AND NOT EXISTS (SELECT 1 FROM user_job_notifications n
--                      WHERE n.user_id = $1 AND n.canonical_id = j.canonical_id)
--
-- The second clause was the new per-user rule. The first was load bearing on the
-- CRAWL path: crawler/store.mark_new writes a company's whole back catalogue
-- notified = TRUE on its first-ever crawl (the silent seed), the crawl path is
-- user-blind by design, and those seeded rows therefore have NO row in this
-- table. Reading this table alone would have put an entire freshly-seeded back
-- catalogue into the digest queue — one discovery sweep once seeded 6,496 rows
-- in a single afternoon.
--
-- 1e took the second of the two options that paragraph named: it REPLACED THE
-- SEED MARKER OUTRIGHT. jobs.seeded now carries "this row is back catalogue",
-- written by the same crawl statement, read by the queue in the exact position
-- notified = FALSE used to occupy. The queue is now
--
--     AND EXISTS     (user_companies for this user and this job's company)
--     AND j.seeded = FALSE
--     AND NOT EXISTS (user_job_notifications for this user and this job)
--
-- which is per-user in the two places that decide, and user-blind in the one
-- that describes a crawl. See notify/store.CANDIDATES_SQL.
--
-- WHAT STILL PUTS ROWS IN THIS TABLE WITHOUT AN EMAIL. The seed is one half of
-- the history hazard; the other is a user who starts tracking a company that
-- ALREADY has stored history — those rows are not seeded (they accumulated at
-- notified = FALSE over weeks) and would be that person's first digest, in bulk.
-- db/watchlist.track_companies creates the pair and records that history as
-- already-notified for them IN ONE TRANSACTION. Such a row means "this person
-- will never be emailed about this job", which is what every row here has always
-- meant; notified_at stays record-keeping and nothing branches on it.
--
-- This table, jobs.seeded and the watch list are now the whole of the decision.
-- jobs.notified is gone; see the block above the DROP in the jobs section for
-- what it meant, why it outlived its last reader by three sub-phases, and what
-- had to be true before it could go.
--
-- No seed row here, for the same reason public.users and user_companies have
-- none: the only user is a real person resolved from MY_EMAIL at runtime and
-- this file cannot name them. scripts/backfill_user_notifications.py is the
-- seed, and it is the highest-risk step in Phase 1 — see its docstring.


-- ===========================================================================
-- ANON AND AUTHENTICATED REACH NOTHING — the PostgREST surface, closed in SQL
-- ===========================================================================
--
-- Phase 2a publishes NEXT_PUBLIC_SUPABASE_ANON_KEY into the browser bundle,
-- because that is what the key is for: the Supabase JS client needs it to run
-- the OAuth handshake. Publishing it is safe only if the key opens nothing but
-- /auth/v1. Before this block it opened rather more than that.
--
-- WHAT WAS ACTUALLY TRUE, measured against the live database on 2026-08-04:
-- seven of the ten tables in this schema had RLS OFF, and the anon role held
-- SELECT, INSERT, UPDATE, DELETE and TRUNCATE on every one of them — 70 grants
-- for anon and 70 more for authenticated. All six sequences carried anon=rwU,
-- where the w is setval. Supabase grants those by default to whatever appears
-- in `public`, and this project never asked it not to, because until now no key
-- that uses them had ever left the server. Neither runtime speaks PostgREST at
-- all: the crawler is asyncpg on the session pooler and the dashboard is
-- postgres.js on the transaction pooler. It was reachable surface that no code
-- path here has ever needed.
--
-- SO THE KEY IS NOT THE BUG AND HIDING IT IS NOT THE FIX. An anon key is meant
-- to be public; what makes it harmless everywhere else is that RLS stands
-- behind it. Three locks are set below, and they fail independently:
--
--   1. ENABLE ROW LEVEL SECURITY, with NO policy on any table. RLS on plus zero
--      policies is deny-all: anon and authenticated match nothing and read
--      nothing. A policy added later is then a deliberate act with a name.
--   2. REVOKE ALL on what exists, so the privilege is not merely unusable but
--      absent — on the eight tables and on the six sequences behind their
--      SERIAL columns.
--   3. ALTER DEFAULT PRIVILEGES, so what does not exist yet arrives closed.
--
-- WHY THE THIRD IS NOT REDUNDANT, and it is the one that matters most going
-- forward. Locks 1 and 2 are statements about the tables that exist today. A
-- table created after this runs inherits Supabase's default grants and arrives
-- OPEN — and Phase 3 is going to create tables (the company picker). Without
-- lock 3 the guard is a tripwire that fires after the hole is already open;
-- with it, the hole never opens. tests/test_rls.py and scripts/check_rls.py
-- both sweep whatever tables exist rather than a fixed list, so they remain the
-- belt to this suspender rather than being replaced by it.
--
-- THE CAVEAT ALTER DEFAULT PRIVILEGES ALWAYS CARRIES: it applies only to
-- objects created by the ROLE THAT RUNS IT, and silently does nothing for
-- anything created by anyone else. So it matters who executes this file.
-- Measured rather than assumed: pg_default_acl's entry for schema `public`,
-- object type TABLES, is set_by = postgres and reads anon=arwdDxtm/postgres.
-- Both DSNs in this project authenticate as postgres.<project-ref>, which IS
-- that role, and scripts/init_db.py applies this file over CRAWLER_DB_URL — so
-- the statements below target exactly the entry that is currently granting
-- anon, and every table either runtime creates is covered. Applying this file
-- as some other role would be a silent no-op, which is why check_rls.py reports
-- the connecting role rather than only the verdict.
--
-- WHY THIS CANNOT BREAK EITHER RUNTIME, and again it is evidence rather than an
-- argument. pg_roles reports rolbypassrls = TRUE for postgres, which owns all
-- ten tables; anon and authenticated have rolbypassrls = FALSE and are not
-- superusers, so RLS binds them and not us. The proof is already in the
-- database and predates this block: public.users has had RLS ON since it was
-- created, carries no policy, and every read in both runtimes has worked the
-- whole of Phase 1. This block reproduces on the other tables a configuration
-- that has been running in production on this one.
--
-- The dashboard toggle (Project Settings -> API -> Exposed schemas) closes the
-- same door and is what unblocks the deploy. It is a console setting on an
-- account, not a property of this database — someone re-exposing `public` in a
-- year would silently undo it, and nothing would fail. These statements are the
-- durable half, and scripts/check_rls.py is what reads the answer back.
--
-- ENUMERATED rather than ALL TABLES IN SCHEMA public, deliberately. The two
-- jobs_notified_backup_* tables are Phase-1d/1g leftovers that already have RLS
-- on; they are not this schema's, they are not this phase's business, and a
-- blanket statement here would quietly adopt them. public.users IS listed even
-- though its RLS is already on: stating it costs a no-op and stops the file
-- implying that the one table holding account identity was somehow exempt.
--
-- SCOPE, stated so the gaps are chosen rather than missed. Schema `public`
-- only. Supabase's own `storage`, `graphql` and `graphql_public` schemas carry
-- the identical default grants and are deliberately untouched — they are not
-- this project's to configure, and `storage` is empty here. FUNCTIONS are
-- included in lock 3 although `public` currently holds none, because a function
-- is how PostgREST exposes RPC and the cost of covering it in advance is one
-- line.
--
-- EVERY STATEMENT BELOW IS A NO-OP ON RE-RUN. ENABLE ROW LEVEL SECURITY on an
-- RLS-enabled table does nothing; REVOKE of a privilege nobody holds does
-- nothing; ALTER DEFAULT PRIVILEGES re-revoking an absent default does nothing.
-- This file stays safe to re-apply against a live database.

-- Lock 1 — RLS on, no policies anywhere, which is deny-all.
ALTER TABLE public.companies              ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.discovered_companies   ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.jobs                   ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.notifications          ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.preferences            ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.user_companies         ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.user_job_notifications ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.users                  ENABLE ROW LEVEL SECURITY;

-- Lock 2a — the privileges on the tables that exist.
REVOKE ALL PRIVILEGES ON public.companies              FROM anon, authenticated;
REVOKE ALL PRIVILEGES ON public.discovered_companies   FROM anon, authenticated;
REVOKE ALL PRIVILEGES ON public.jobs                   FROM anon, authenticated;
REVOKE ALL PRIVILEGES ON public.notifications          FROM anon, authenticated;
REVOKE ALL PRIVILEGES ON public.preferences            FROM anon, authenticated;
REVOKE ALL PRIVILEGES ON public.user_companies         FROM anon, authenticated;
REVOKE ALL PRIVILEGES ON public.user_job_notifications FROM anon, authenticated;
REVOKE ALL PRIVILEGES ON public.users                  FROM anon, authenticated;

-- Lock 2b — the sequences behind the SERIAL columns. Enumerated for the same
-- reason the tables are, and they are a real exposure rather than tidiness:
-- anon=rwU includes UPDATE, which on a sequence is setval — an anon caller
-- could have reset companies_id_seq and made the next insert collide.
-- public.users and public.jobs have no sequence (uuid and text keys).
REVOKE ALL PRIVILEGES ON SEQUENCE public.companies_id_seq              FROM anon, authenticated;
REVOKE ALL PRIVILEGES ON SEQUENCE public.discovered_companies_id_seq   FROM anon, authenticated;
REVOKE ALL PRIVILEGES ON SEQUENCE public.notifications_id_seq          FROM anon, authenticated;
REVOKE ALL PRIVILEGES ON SEQUENCE public.preferences_id_seq            FROM anon, authenticated;
REVOKE ALL PRIVILEGES ON SEQUENCE public.user_companies_id_seq         FROM anon, authenticated;
REVOKE ALL PRIVILEGES ON SEQUENCE public.user_job_notifications_id_seq FROM anon, authenticated;

-- Lock 3 — what does not exist yet arrives closed. See the caveat above: this
-- binds objects created by the role that RUNS it, which is postgres, which is
-- the role both DSNs use and the role pg_default_acl already records as the
-- grantor of the entries being revoked here.
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES    FROM anon, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON SEQUENCES FROM anon, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON FUNCTIONS FROM anon, authenticated;


-- ===========================================================================
-- public.users.auth_user_id — the join to Supabase Auth, Phase 2a
-- ===========================================================================
--
-- Phase 2a replaces the single-account ALLOWED_EMAIL gate with real Supabase
-- Auth, so a Google sign-in creates a row in auth.users and this table needs a
-- matching one. This column is the join, and it exists instead of the obvious
-- alternative for one concrete reason: THE OBVIOUS ALTERNATIVE WOULD HAVE
-- DESTROYED THE EXISTING ACCOUNT.
--
-- The tutorial pattern is a trigger on auth.users doing
--
--     INSERT INTO public.users (id, email) VALUES (NEW.id, NEW.email)
--       ON CONFLICT (id) DO NOTHING;
--
-- and against this database that is a trap. The one real account predates auth
-- entirely: its id came from gen_random_uuid() in 1a and has no relationship to
-- any auth.users id, while its email is the address that person will sign in
-- with. So the INSERT conflicts on EMAIL, not on id — users_email_lower_idx and
-- the plain UNIQUE both fire, ON CONFLICT (id) does not catch it, and the
-- statement raises. Inside a trigger that raise happens in GoTrue's own signup
-- transaction, which means the sign-in is refused outright with an opaque
-- "Database error saving new user". The account holding 50 tracked companies
-- and 7,707 notification rows would have been unreachable, and the error would
-- have named none of that.
--
-- WHAT THIS COLUMN DOES INSTEAD. lib/link-user.ts resolves in three branches,
-- in one transaction, on the authenticated request path rather than in a
-- trigger:
--
--   1. auth_user_id already set   -> return that public.users.id. The steady
--                                    state, one indexed lookup, and what makes
--                                    branch 2 run exactly once ever.
--   2. lower(email) matches       -> UPDATE ... SET auth_user_id, return the
--                                    EXISTING id. This is the branch that
--                                    adopts the Phase-1 account rather than
--                                    duplicating it.
--   3. neither                    -> INSERT with id = auth_user_id = the auth
--                                    uid, plus a preferences row, is_active
--                                    FALSE.
--
-- WHY NOT REKEY THE OLD ROW TO ITS auth.users id. That was the other option and
-- it would give one uniform invariant — public.users.id = auth.users.id for
-- everybody. It was rejected because the old id is referenced by 7,758 rows
-- across three tables (preferences, user_companies, user_job_notifications) and
-- every one of those foreign keys is declared ON DELETE CASCADE, not ON UPDATE
-- CASCADE — so the rewrite would be a hand-written multi-table transaction
-- against the only account that exists, to buy a property nothing currently
-- reads. The Phase-1 foreign keys reference public.users(id) whatever value it
-- holds; they do not care whether it equals an auth uid.
--
-- WHAT THE EQUALITY IS ACTUALLY WORTH, so the asymmetry is chosen rather than
-- inherited: RLS policies are written as auth.uid() = user_id, and this project
-- has no policies (see the block above — RLS is on precisely so that there are
-- none and everything is denied). Branch 3 keeps id = auth uid for every future
-- account anyway, so the asymmetry is exactly one row, it is queryable
-- (WHERE id <> auth_user_id), and adopting the rekey later stays possible.
--
-- NULLABLE, like preferences.user_id before it and for the same reason: the
-- value cannot be known when the column is added. Every row is NULL until its
-- owner signs in once. NULL therefore means "this account has never
-- authenticated", which is a true statement about the Phase-1 account until the
-- first Google sign-in and is why there is no backfill script for this one.
--
-- UNIQUE, and that is load bearing rather than tidy: it is what stops two
-- public.users rows claiming the same Supabase identity if branch 3 ever ran
-- twice concurrently. Branch 3's ON CONFLICT DO NOTHING plus re-select handles
-- the race; this constraint is what makes the race impossible to lose silently.
-- A partial index, so the many NULLs do not collide — in Postgres NULLs are
-- distinct under a plain UNIQUE anyway, but saying WHERE auth_user_id IS NOT
-- NULL states the intent and keeps the index to the rows that have a value.
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS auth_user_id UUID;

CREATE UNIQUE INDEX IF NOT EXISTS users_auth_user_id_idx
    ON public.users (auth_user_id)
 WHERE auth_user_id IS NOT NULL;

-- No FOREIGN KEY to auth.users, deliberately, and this is the one place this
-- schema declines a reference it could write. Three reasons, the first
-- decisive: auth.users is Supabase's table, migrated by Supabase on their
-- schedule, and a foreign key from our schema into theirs makes their migration
-- our outage. Second, db/schema.sql is applied by the crawler's role over the
-- session pooler and is meant to be re-runnable against a database where the
-- auth schema may not even be readable. Third, the integrity it would buy is
-- already covered: branch 1 of the link only ever writes an id that GoTrue just
-- authenticated, and a stale auth_user_id pointing at a deleted auth user
-- simply falls through to branch 2 and re-links by email.
