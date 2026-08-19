"use client";

import {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

import { CompanyCardHead } from "@/components/CompanyCardHead";
import { CompanyJobs } from "@/components/CompanyJobs";
import { JobLine, type Mode } from "@/components/JobLine";
import { defaultSortMode, sortCards, SORT_OPTIONS, type SortMode } from "@/lib/cards";
import type { CompanyCard } from "@/lib/companies";
import type { JobView } from "@/lib/jobs";
import { CATEGORIES } from "@/lib/labels";

/**
 * The company list, and the one collapsible thing both screens are made of.
 *
 * ONE COMPONENT, TWO PAGES
 * ------------------------
 * Home and Jobs differ by a single prop. `mode` picks which job set a company
 * fetches when it opens — the digest filter or everything — and picks the two
 * or three details each job shows. Nothing else about the two screens is
 * different, so nothing else is duplicated: one expand animation to get right,
 * one loading state, one empty message, one place where a broken link would be.
 *
 * ONE COMPONENT, TWO LOOKS — THE CARD-GRID SLICE
 * ----------------------------------------------
 * `variant` picks the SHAPE: `list` is the collapsible row this has always been
 * and is what /jobs still renders, `grid` is Home's card grid. It is a second
 * PRESENTATION, not a second list, the same way WatchlistPicker gained
 * `variant="search"` in the reskin without gaining a second picker — and the
 * distinction is the whole reason this was not built as a new component. Every
 * piece of wiring below is shared verbatim between the two: the expand fetch and
 * its once-only cache, the optimistic Applied tick and its rollback, the card
 * replacement on a category edit, the one comparator that sinks applied jobs. A
 * new CompanyGrid would have had to re-earn all of it, and the way that goes
 * wrong is silently — a rollback that does not roll back, a count that stops
 * agreeing with the list under it.
 *
 * It defaults to `list`, so /jobs is unchanged by omission rather than by
 * remembering to pass something.
 *
 * WHY THE JOBS ARE FETCHED AND NOT PASSED IN
 * ------------------------------------------
 * The server already knows every job it would need. Sending them all with the
 * page would be ~790 rows of JSON in the HTML to render fifty numbers, and
 * forty-nine of those fifty cards stay closed. So the page ships counts, and a
 * card asks for its own jobs the first time it is opened — once; the answer is
 * kept, so reopening is instant and closing costs nothing.
 *
 * `preload` CHANGES WHEN THAT REQUEST FIRES AND NOTHING ELSE. A bounded screen
 * can ask for every card's jobs to be fetched in the background after the list
 * paints, so the first open is instant too. It is the same route, the same
 * per-company request and the same state it lands in — the page still ships
 * counts, and the HTML is still fifty numbers. Read that prop's comment before
 * passing it: the default is off because the argument for it is a row cap.
 */

// Re-exported from the row component, which is where the two screens actually
// diverge. Importers of this file kept the name they had.
export type { Mode };

/** Which shape the same list is drawn in. */
export type Variant = "list" | "grid";

export function CompanyList({
  initial,
  mode,
  empty,
  variant = "list",
  now,
  preload = false,
}: {
  initial: CompanyCard[];
  mode: Mode;
  /**
   * What to say when there is nothing to list, since both screens became
   * watch-list-scoped. It is a prop rather than a string in here because the two
   * callers now have genuinely different reasons for an empty list — "you watch
   * nothing" is a thing a person fixes on /watchlist, and the old message ("run
   * seed_companies.py") answered a question only the operator of an empty
   * database could have. Defaulted, so an empty catalog still explains itself.
   *
   * ON THE GRID IT IS VERY NEARLY UNREACHABLE, and deliberately so: Home fills to
   * GRID_CAP with the catalog, so the only way to see this is a database with no
   * companies in it at all. There is no "nothing matched" empty state, because
   * nothing matching is what a card saying 0 is for.
   */
  empty?: ReactNode;
  variant?: Variant;
  /**
   * The instant the page was rendered, ISO, threaded from the server.
   *
   * NOT `new Date()` INSIDE THE FRESHNESS TAG, and this is a hydration bug rather
   * than a preference. The server renders "59m ago" into the HTML, the browser
   * hydrates a minute later and computes "1h ago", and React reports a mismatch
   * on a screen that is otherwise correct. One instant, decided once, by the
   * clock that also owns `first_seen_at`.
   */
  now?: string;
  /**
   * Fetch every listed company's jobs in the background once the list has
   * painted, so that opening a card reads state that is already there instead of
   * starting a request and showing "Loading…".
   *
   * OFF BY DEFAULT, AND THAT DEFAULT IS A SCALING FENCE RATHER THAN TASTE. This
   * is correct on Home for exactly one reason: Home is capped at GRID_CAP (50)
   * cards, so "all of them" is a known, small, bounded number of requests, and
   * the cap is what makes the arithmetic safe. It is NOT a better way to load a
   * company list in general. A search screen returns an unbounded result set —
   * two hundred companies is an ordinary answer there — and turning this on for
   * it would fire two hundred queries to open perhaps one card.
   *
   * So it is opt-in at the CALLER: a page has to state that its list is bounded
   * before it gets this, and a new screen inherits the lazy behaviour by saying
   * nothing. Do not flip the default, and do not pass this from a screen whose
   * row count is decided by a query rather than by a cap.
   */
  preload?: boolean;
}) {
  const [rows, setRows] = useState(initial);
  const [error, setError] = useState<string | null>(null);
  /**
   * WHICH ORDER THE CARDS ARE IN, and where the default comes from.
   *
   * Derived from the data rather than stored anywhere: lib/cards.defaultSortMode
   * opens on latest-first while this account has a match no digest has sent yet,
   * and on highest-count-first otherwise. Read that function for why it is not
   * monotonic and why that was the accepted trade.
   *
   * The choice is NOT persisted — no column, no localStorage, no ?sort= — so a
   * reload returns to the computed default. The default is itself dynamic and
   * self-correcting, which is exactly the thing a remembered override would stop
   * working.
   */
  const [sort, setSort] = useState<SortMode>(() => defaultSortMode(initial));
  const listId = useId();

  // Replace one card in place. `rows` is a set here, never an order: the grid's
  // order is computed below and the list's is the server's.
  const replace = useCallback((next: CompanyCard) => {
    setRows((rs) => rs.map((r) => (r.id === next.id ? next : r)));
  }, []);

  /**
   * The order, and the rule that stopped being "never recompute".
   *
   * The row list froze its order after the first render, so that a card could not
   * jump out from under the cursor because the tag next to it had been edited.
   * That rule survives here in the form that actually mattered: every key the
   * three comparators read — `match_jobs`, `latest_match_at`, `name` — is a field
   * NO control on this page can change. The two things a person can edit from a
   * card are its category and its Applied ticks, and neither is sorted on. So a
   * card still cannot move under the cursor; it moves when, and only when,
   * somebody asks for a different order.
   *
   * The list variant is left exactly as the server sent it — /jobs is a reference
   * you look things up in, and a reference should be where you left it.
   */
  const ordered = useMemo(
    () => (variant === "grid" ? sortCards(rows, sort) : rows),
    [rows, sort, variant],
  );

  if (!rows.length) {
    return (
      <p className="msg warn">
        {empty ?? (
          <>
            No companies yet. Add one on the Add tab, or run{" "}
            <code>python scripts/seed_companies.py</code>.
          </>
        )}
      </p>
    );
  }

  return (
    <>
      {error ? <p className="msg bad">{error}</p> : null}
      {variant === "grid" ? (
        <div className="grid-bar">
          <label className="sort-select">
            <span className="muted small">Sort</span>
            <select
              value={sort}
              aria-label="Sort companies"
              onChange={(e) => setSort(e.target.value as SortMode)}
            >
              {SORT_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
        </div>
      ) : null}
      <ul className={variant === "grid" ? "company-grid" : "companies"} id={listId}>
        {ordered.map((company) => (
          <CompanyRow
            key={company.id}
            company={company}
            mode={mode}
            variant={variant}
            now={now}
            preload={preload}
            onPatched={replace}
            onError={setError}
          />
        ))}
      </ul>
    </>
  );
}

/**
 * One company: a header that is always visible, and a job list that is not.
 *
 * The header is deliberately NOT one big click target in the LIST variant. The
 * disclosure button covers the name and the counts; the category tag and the
 * tracked pill sit outside it, as siblings. Nesting a <select> inside a <button>
 * is invalid HTML and behaves accordingly — the row would open every time the tag
 * was changed.
 *
 * THE GRID VARIANT ANSWERS THAT DIFFERENTLY, and the same rule is what decides
 * it. A card IS one click target — the whole face opens it, which is what a card
 * is for — so the two controls that cannot live inside a <button> move DOWN into
 * the panel rather than sitting beside the face where they would have to punch a
 * hole in it. Nothing is lost: the category select and the read-only tracked pill
 * are both still rendered, still exactly once, and still in the markup of a
 * closed card (the panel is `hidden`, not absent). Changing a category is now one
 * click further away, which is the right price for a face that can be scanned
 * fifty at a time.
 */
function CompanyRow({
  company,
  mode,
  variant,
  now,
  preload,
  onPatched,
  onError,
}: {
  company: CompanyCard;
  mode: Mode;
  variant: Variant;
  now?: string;
  /** See CompanyList's prop of the same name — bounded lists only. */
  preload?: boolean;
  onPatched: (next: CompanyCard) => void;
  onError: (message: string | null) => void;
}) {
  const [open, setOpen] = useState(false);
  const [jobs, setJobs] = useState<JobView[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const panelId = useId();

  const shown = mode === "matches" ? company.match_jobs : company.open_jobs;

  /**
   * Applied jobs sink to the bottom of the list, in the browser rather than in
   * the ORDER BY.
   *
   * It has to happen here anyway: ticking a box must move the row immediately,
   * and the list is not refetched when it does. Putting the same rule in the SQL
   * as well would be a second ordering that the client overrides a moment later
   * — one definition, in the place that has to own it.
   *
   * Array.prototype.sort is stable, so within each group the server's order —
   * newest first, then title — is exactly preserved. This is the only comparator,
   * and it stays the only one on the grid: CompanyJobs groups these rows by city
   * WITHOUT reordering them, so an applied job sinks within its own city and
   * nothing else moves.
   */
  const ordered = useMemo(() => {
    if (jobs === null) return null;
    return [...jobs].sort((a, b) => Number(a.applied) - Number(b.applied));
  }, [jobs]);

  /**
   * Ask for this company's jobs — the same one request the expand has always
   * made, lifted out of the click handler so that opening a card and preloading
   * one are the same code path rather than two.
   *
   * ONCE, whoever asks first. The `started` ref rather than the `loading` state
   * because the two callers can arrive in the same tick — a click landing while
   * the preload's fetch is still in flight must join it, not race it — and state
   * read inside an async function is a snapshot. The route, the query and the
   * shape of what lands in `jobs` are untouched; only the moment it fires moved.
   */
  const started = useRef(false);
  const loadJobs = useCallback(async () => {
    if (started.current) return;
    started.current = true;

    setLoading(true);
    setLoadError(null);
    try {
      const res = await fetch(`/api/companies/${company.id}/jobs?filter=${mode}`);
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || data.error || `HTTP ${res.status}`);
      setJobs(data.jobs as JobView[]);
    } catch (err) {
      // Let it be retried by opening the card, rather than leaving a preload
      // that failed in the background as a permanently broken panel.
      started.current = false;
      setLoadError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, [company.id, mode]);

  /**
   * The eager fetch, on the screens that asked for it.
   *
   * useEffect and NOT useLayoutEffect, which is the whole point of the slice:
   * effects run AFTER the browser has painted, so the grid — which already has
   * every count it needs from the server — is on screen before the first request
   * leaves. The jobs fill in behind it. A card opened in that window still shows
   * the ordinary loading state, for that card only, and then never again.
   *
   * Fifty of these mount at once on Home; the browser's own per-origin
   * connection limit is what paces them, and GRID_CAP is what makes fifty the
   * ceiling. See the `preload` prop's comment for why this must not be turned on
   * for an unbounded list.
   */
  useEffect(() => {
    if (preload) void loadJobs();
  }, [preload, loadJobs]);

  async function toggleOpen() {
    const next = !open;
    setOpen(next);
    if (!next || jobs !== null) return;
    await loadJobs();
  }

  /**
   * Tick or untick "applied" on one job.
   *
   * Optimistic like the category select below and for the same reason — the round
   * trip to Tokyo is long enough that a checkbox which waits for it feels broken
   * — but with one extra step: the response carries the re-read row, and that is
   * what finally lands in state, because `applied_at` is the database clock's to
   * decide and the browser cannot guess it.
   *
   * The company's "N applied" note is nudged in the same breath. It was computed
   * server-side over the match set, so a tick that did not move it would leave
   * the card disagreeing with the list directly underneath it until a reload.
   * The spread carries every other field of the card — the chip row, the
   * freshness tag, the queued count — through untouched, which is what stops an
   * optimistic patch from blanking the face of the card being ticked.
   */
  async function toggleApplied(job: JobView, applied: boolean) {
    const before = jobs;
    const beforeCard = company;
    onError(null);

    setJobs((current) =>
      (current ?? []).map((j) => (j.canonical_id === job.canonical_id ? { ...j, applied } : j)),
    );
    onPatched({
      ...company,
      applied_jobs: Math.max(0, company.applied_jobs + (applied ? 1 : -1)),
    });

    try {
      const res = await fetch("/api/jobs/applied", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ canonical_id: job.canonical_id, applied }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || data.error || `HTTP ${res.status}`);

      const saved = data.job as JobView;
      setJobs((current) =>
        (current ?? []).map((j) =>
          // Neither `matches` nor `user_notified` is a column, so the row the
          // PATCH returns does not carry either: that statement writes two
          // columns and reads back two columns, which is what keeps it provably
          // unable to touch anything else. Both are kept from the row this list
          // was rendered with, rather than dropping the badge off the row that
          // was just ticked. Ticking "applied" says nothing about whether the
          // filter still likes the job or whether a digest has mentioned it.
          j.canonical_id === saved.canonical_id
            ? { ...saved, matches: j.matches, user_notified: j.user_notified }
            : j,
        ),
      );
    } catch (err) {
      setJobs(before);
      onPatched(beforeCard);
      onError(
        `Could not mark "${job.title}" as ${applied ? "applied" : "not applied"}: ` +
          `${err instanceof Error ? err.message : String(err)}`,
      );
    }
  }

  /**
   * The one editable field. Optimistic — the round trip to Tokyo is long enough
   * that a control which waits for it feels broken and gets tapped twice — and
   * it rolls back to the card we started from if the write fails.
   *
   * It carried `tracked` too until that write was removed as a security fix; the
   * shape is kept as an object so the next genuinely per-user field is an
   * addition rather than a rewrite. See lib/companies.updateCompany.
   */
  async function patch(body: { category?: string }) {
    const before = company;
    setPending(true);
    onError(null);
    onPatched({ ...company, ...body });

    try {
      const res = await fetch(`/api/companies/${company.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || data.error || `HTTP ${res.status}`);
      onPatched(data.company as CompanyCard); // trust the server over the guess
    } catch (err) {
      onPatched(before);
      onError(
        `Could not update ${before.name}: ${err instanceof Error ? err.message : String(err)}`,
      );
    } finally {
      setPending(false);
    }
  }

  /**
   * The two controls that may not live inside a <button>, and the untracked note.
   *
   * One definition, rendered in two places: beside the header in the list, inside
   * the panel on a card. Shared rather than copied because the tracked pill is
   * the subject of a security assertion — it must be SHOWN and must not be
   * writable — and a copy is how the second one quietly becomes a switch again.
   */
  const controls = (
    <div className="company-controls">
      <label className="cat">
        <span className="sr-only">Category for {company.name}</span>
        <select
          value={company.category ?? "unknown"}
          disabled={pending}
          onChange={(e) => patch({ category: e.target.value })}
        >
          {CATEGORIES.map((c) => (
            <option key={c} value={c}>
              {c}
            </option>
          ))}
        </select>
      </label>
      {/*
        READ-ONLY, AND IT USED TO BE A SWITCH. This pill sat inside a
        <label> wrapping a checkbox that PATCHed companies.tracked — the
        crawler's catalog-wide flag, one shared value for the whole install.
        Any signed-in account could therefore stop a company being crawled
        for everybody. The write is gone from the route and from the SQL (see
        lib/companies.updateCompany), and the control is gone from here, so
        there is no longer a writable control anywhere that maps to it.

        The pill stays because the fact is still worth showing: an untracked
        company's jobs are stale, and the note below says so. A span, with no
        handler and no input — this must not become interactive again without
        an answer to "whose flag is it", and the answer is: not the reader's.

        THE CARD-GRID SLICE MOVED IT INTO THE PANEL AND CHANGED NOTHING ELSE
        ABOUT IT. It is still rendered, still on every company, still a span.
      */}
      <span
        className={company.tracked ? "tag on" : "tag off"}
        title={
          company.tracked
            ? "Crawled hourly. This is the catalog-wide crawl flag, not a per-account setting."
            : "Not being crawled. This is the catalog-wide crawl flag, not a per-account setting."
        }
      >
        {company.tracked ? "tracked" : "off"}
      </span>
    </div>
  );

  const untrackedNote = company.tracked ? null : (
    <p className="untracked-note muted small">
      Not being crawled. These {company.open_jobs} job
      {company.open_jobs === 1 ? "" : "s"} are whatever was stored the last time it was
      tracked, and nothing new will arrive until it is tracked again. That is a
      catalog-wide setting for this install, changed where the crawler runs — not from
      here.
    </p>
  );

  /** The jobs themselves, once a company is open. Identical in both variants
   *  except for the grouping, which only the grid asks for. */
  const body = (
    <>
      {loading ? <p className="muted small">Loading…</p> : null}
      {loadError ? <p className="msg bad">Could not load jobs: {loadError}</p> : null}
      {!loading && !loadError && ordered !== null ? (
        ordered.length ? (
          variant === "grid" ? (
            <CompanyJobs jobs={ordered} mode={mode} onApplied={toggleApplied} />
          ) : (
            <ul className="jobs">
              {ordered.map((job) => (
                <JobLine
                  key={job.canonical_id}
                  job={job}
                  mode={mode}
                  onApplied={toggleApplied}
                />
              ))}
            </ul>
          )
        ) : (
          <p className="muted small">
            {mode === "matches"
              ? "No jobs matching your keywords."
              : "No open jobs stored for this company."}
          </p>
        )
      ) : null}
      {!loading && !loadError && jobs !== null && jobs.length !== shown ? (
        <p className="muted small">
          The row said {shown}; the database now says {jobs.length}. The crawler has run since
          this page loaded — reload to refresh the counts.
        </p>
      ) : null}
    </>
  );

  if (variant === "grid") {
    return (
      <li
        className={`company-card${open ? " open" : ""}${company.tracked ? "" : " untracked"}`}
      >
        {/*
          THE WHOLE FACE IS THE BUTTON. CompanyCardHead is deliberately a
          label — no select, no input, no link — which is what makes that legal
          and is why the two real controls are in the panel below.
        */}
        <button
          type="button"
          className="card-open"
          aria-expanded={open}
          aria-controls={panelId}
          onClick={toggleOpen}
        >
          <CompanyCardHead company={company} now={now ? new Date(now) : undefined} />
          <span className="card-chev" aria-hidden="true">
            {open ? "▾" : "▸"}
          </span>
        </button>

        <div id={panelId} hidden={!open} className="card-body">
          {untrackedNote}
          {body}
          {controls}
        </div>
      </li>
    );
  }

  return (
    <li className={`company${company.tracked ? "" : " untracked"}`}>
      <div className="company-head">
        <button
          type="button"
          className="disclose"
          aria-expanded={open}
          aria-controls={panelId}
          onClick={toggleOpen}
        >
          <span className="chev" aria-hidden="true">
            {open ? "▾" : "▸"}
          </span>
          <span className="stack">
            <span className="company-name">
              {company.name}
              <span className="tag">{company.ats}</span>
              {company.india_scoped ? (
                <span
                  className="tag"
                  title="Filtered at source: this company's rows are deliberately India-only and therefore partial."
                >
                  india-scoped
                </span>
              ) : null}
            </span>
            <span className="muted small mono">{company.token}</span>
          </span>
          <span className="counts">
            {mode === "matches" ? (
              <span className={`count${company.match_jobs ? " hit" : ""}`}>
                <strong className="num">{company.match_jobs}</strong>
                <span className="muted small">for you</span>
              </span>
            ) : null}
            {/*
              Shown only when there is one. A permanent "0 applied" on every
              company would be eleven zeroes reporting that nothing has happened,
              which is worse than silence; a number that appears when it becomes
              true reads as progress.
            */}
            {mode === "matches" && company.applied_jobs > 0 ? (
              <span
                className="count applied-count"
                title={`${company.applied_jobs} of the ${company.match_jobs} jobs that fit you here are ticked as applied.`}
              >
                <strong className="num">{company.applied_jobs}</strong>
                <span className="muted small">applied</span>
              </span>
            ) : null}
            <span className="count">
              <strong className="num">{company.open_jobs}</strong>
              <span
                className="muted small"
                title={`${company.total_jobs} rows stored in all, ${
                  company.total_jobs - company.open_jobs
                } of them closed`}
              >
                total jobs
              </span>
            </span>
          </span>
        </button>

        {controls}
      </div>

      {untrackedNote}

      <div id={panelId} hidden={!open} className="company-body">
        {body}
      </div>
    </li>
  );
}
