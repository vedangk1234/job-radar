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
  notified         BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS jobs_company_status_idx ON jobs (company_id, status);

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
-- Exactly one row, forever. The CHECK plus the fixed default is what enforces
-- that — every INSERT lands on id=1 and collides with the row already there,
-- so "the preferences" is never an ambiguous question.
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
  ARRAY['blr', 'pune', 'hyd', 'india-unclear'],
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
