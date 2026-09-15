# Crawl-selection UI — design

**Date:** 2026-09-15
**Problem:** After `promote_india.py` mass-promoted ~650 boards, `companies.tracked`
ballooned to ~700 and the hourly crawl (`crawl.py` → `WHERE tracked = TRUE`)
exceeded the 20-minute cron timeout and got cancelled. Data was fixed (untracked
the 650 promoted boards → 50 tracked), but there is no way in the app to manage
which companies are crawled, and the operator wants to curate the set (up to 150)
from the dashboard.

## Decisions (from brainstorming)
- **Axis:** the feature edits `companies.tracked` — the shared, catalog-wide crawl
  flag. Selecting a company = tracked = crawled hourly + shown by default. `crawl.py`
  is **unchanged** (already crawls `WHERE tracked = TRUE`).
- **Access:** any logged-in user may toggle (operator's explicit, informed choice —
  the app has open sign-up since Phase 2a; the shared-flag risk was explained and
  accepted). No operator identity is introduced.
- **Cap:** at most **150** companies tracked at once (global). Enforced server-side.
- **Retire "watch up to 5":** `user_companies` is not removed (keeps `/watchlist`
  and its tests intact), but onboarding's company step switches to the tracked
  picker so signup has one company concept.

## Components

### Backend
- `lib/labels.ts`: `export const TRACKED_CAP = 150;` (client-safe, no db import).
- `lib/companies.ts`:
  - `trackedCount(): Promise<number>` — `SELECT count(*) … WHERE tracked`.
  - `setTracked(companyId, tracked): Promise<void>` — one transaction:
    global advisory lock (constant key, cap is global), verify company exists,
    UPDATE tracked, and when turning **on** re-count and throw `TrackedCapError`
    (rolls back) if it would exceed `TRACKED_CAP`. Mirrors `watchlist.watchCompany`.
  - `TrackedCapError` (carries cap + current count).
  - `updateCompany` stays category-only; comments updated (no longer "sealed").
- `app/api/companies/[id]/route.ts`: accept `{ tracked: boolean }`. Calls
  `setTracked`; `TrackedCapError` → **409** with a human message; returns the
  re-read `companyCard`. The old 400 refusal of `tracked` is removed.

### Frontend
- `CompanyList.tsx`: the read-only tracked `<span>` becomes a **toggle button**
  that PATCHes `{ tracked }`, optimistic with rollback (same shape as the category
  patch). New props `cap` + `trackedCount`; the list keeps a live tracked count in
  state and disables **Track** (not Untrack) once at the cap. Used by Home & Jobs,
  so both get the toggle.
- `components/CompanyManager.tsx` (new client component): the management surface.
  All companies, **50 per page** with pager, sorts **A–Z / Z–A / Most jobs /
  Most relevant** (relevant = `match_jobs`), a `TRACKED`/`UNTRACKED` badge and a
  toggle per row, and a live **"N / 150 tracked"** counter. Reuses the PATCH
  `{ tracked }` endpoint via a small shared client helper.
- `app/companies/page.tsx` (new): `requirePage()`, loads `listCompanyCards(userId)`
  + `trackedCount()`, renders `CompanyManager`.
- `Nav.tsx`: add a **Companies** tab.
- `OnboardingSetup.tsx`: the "Companies to watch" section becomes an optional
  "Companies to crawl" section rendering `CompanyManager` (skippable — does not
  gate Save; the onboarding gate/`PUT /api/preferences` are untouched).

### Tests / verification
- Rewrite `tests/test_tracked_write_sealed.py` → the new invariant: `tracked` is
  written **only** by `setTracked`/`upsertCompany` in `lib/companies.ts` (nothing
  else under `dashboard/src`), the route **accepts** `tracked`, and the cap is
  enforced. (Keeps a real guard; drops the "sealed" assertions the feature reverses.)
- Update `ui-check` assertions that pin the read-only pill / "no Track control".
- Verify: `npm run typecheck`, `npm run build`, `pytest` (guard test). ui-check /
  live-DB checks noted where they need a running server.

## Out of scope (follow-up)
- Removing `/watchlist` + `WatchlistPicker` + `user_companies` entirely.
- Making the crawl union-of-user-selections (multi-tenant); `tracked` stays shared.
- Real logo fetching, search box on the manager beyond the sorts above.
