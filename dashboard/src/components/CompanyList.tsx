"use client";

import { useCallback, useId, useMemo, useState } from "react";

import { JobLine, type Mode } from "@/components/JobLine";
import type { CompanyCard } from "@/lib/companies";
import type { JobView } from "@/lib/jobs";
import { CATEGORIES } from "@/lib/labels";

/**
 * The company list, and the one collapsible row both screens are made of.
 *
 * ONE COMPONENT, TWO PAGES
 * ------------------------
 * Home and Jobs differ by a single prop. `mode` picks which job set a row
 * fetches when it opens — the digest filter or everything — and picks the two
 * or three details each job shows. Nothing else about the two screens is
 * different, so nothing else is duplicated: one expand animation to get right,
 * one loading state, one empty message, one place where a broken link would be.
 *
 * WHY THE JOBS ARE FETCHED AND NOT PASSED IN
 * ------------------------------------------
 * The server already knows every job it would need. Sending them all with the
 * page would be ~790 rows of JSON in the HTML to render twelve numbers, and
 * eleven of those twelve companies stay collapsed. So the page ships counts,
 * and a row asks for its own jobs the first time it is opened — once; the
 * answer is kept, so reopening is instant and closing costs nothing.
 */

// Re-exported from the row component, which is where the two screens actually
// diverge. Importers of this file kept the name they had.
export type { Mode };

export function CompanyList({ initial, mode }: { initial: CompanyCard[]; mode: Mode }) {
  const [rows, setRows] = useState(initial);
  const [error, setError] = useState<string | null>(null);

  // Replace one card in place. Order is never recomputed after the first
  // render: a row must not jump to a different position under the cursor
  // because the tag next to it was edited.
  const replace = useCallback((next: CompanyCard) => {
    setRows((rs) => rs.map((r) => (r.id === next.id ? next : r)));
  }, []);

  if (!rows.length) {
    return (
      <p className="msg warn">
        No companies yet. Add one on the Add tab, or run{" "}
        <code>python scripts/seed_companies.py</code>.
      </p>
    );
  }

  return (
    <>
      {error ? <p className="msg bad">{error}</p> : null}
      <ul className="companies">
        {rows.map((company) => (
          <CompanyRow
            key={company.id}
            company={company}
            mode={mode}
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
 * The header is deliberately NOT one big click target. The disclosure button
 * covers the name and the counts; the category tag and the tracked switch sit
 * outside it, as siblings. Nesting a <select> inside a <button> is invalid HTML
 * and behaves accordingly — the row would open every time the tag was changed.
 */
function CompanyRow({
  company,
  mode,
  onPatched,
  onError,
}: {
  company: CompanyCard;
  mode: Mode;
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
   * newest first, then title — is exactly preserved. This is the only comparator.
   */
  const ordered = useMemo(() => {
    if (jobs === null) return null;
    return [...jobs].sort((a, b) => Number(a.applied) - Number(b.applied));
  }, [jobs]);

  async function toggleOpen() {
    const next = !open;
    setOpen(next);
    if (!next || jobs !== null || loading) return;

    setLoading(true);
    setLoadError(null);
    try {
      const res = await fetch(`/api/companies/${company.id}/jobs?filter=${mode}`);
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || data.error || `HTTP ${res.status}`);
      setJobs(data.jobs as JobView[]);
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }

  /**
   * Tick or untick "applied" on one job.
   *
   * Optimistic like the company switch above and for the same reason — the round
   * trip to Tokyo is long enough that a checkbox which waits for it feels broken
   * — but with one extra step: the response carries the re-read row, and that is
   * what finally lands in state, because `applied_at` is the database clock's to
   * decide and the browser cannot guess it.
   *
   * The company's "N applied" note is nudged in the same breath. It was computed
   * server-side over the match set, so a tick that did not move it would leave
   * the header disagreeing with the list directly underneath it until a reload.
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
   * One PATCH for both editable fields. Optimistic — the round trip to Tokyo is
   * long enough that a switch which waits for it feels broken and gets tapped
   * twice — and it rolls back to the card we started from if the write fails.
   */
  async function patch(body: { tracked?: boolean; category?: string }) {
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
          <label className="switch">
            <input
              type="checkbox"
              checked={company.tracked}
              disabled={pending}
              onChange={() => patch({ tracked: !company.tracked })}
              aria-label={`Track ${company.name}`}
            />
            <span className={company.tracked ? "tag on" : "tag off"}>
              {company.tracked ? "tracked" : "off"}
            </span>
          </label>
        </div>
      </div>

      {company.tracked ? null : (
        <p className="untracked-note muted small">
          Not being crawled. These {company.open_jobs} job
          {company.open_jobs === 1 ? "" : "s"} are whatever was stored the last time it was
          tracked, and nothing new will arrive until the switch goes back on.
        </p>
      )}

      <div id={panelId} hidden={!open} className="company-body">
        {loading ? <p className="muted small">Loading…</p> : null}
        {loadError ? <p className="msg bad">Could not load jobs: {loadError}</p> : null}
        {!loading && !loadError && ordered !== null ? (
          ordered.length ? (
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
      </div>
    </li>
  );
}
